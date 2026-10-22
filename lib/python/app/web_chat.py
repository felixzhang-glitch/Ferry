from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.commands import (build_help_text, command_catalog, parse_memory_command, process_command,
                          parse_reminder_command, parse_daily_command, execute_daily_command)
from app.skills import build_skill_summary
from app.web_chat_store import ChatError, MAX_OUTPUT_BYTES, WebChatStore
from core.agent.types import AgentClientCancelled, AgentClientError
from core.agent.pi_cli import sanitize_tool_summary
from core.session.manager import apply_pending_notices, format_file_notice
from core.session.message_queue import SessionMessageQueue
from observability.telemetry import emit, observe_run, set_status

logger = logging.getLogger(__name__)
_TURN_TIMEOUT = 600.0
_ROOTS = ("data", "root", "home", "tmp", "var", "etc", "srv", "opt", "usr", "run", "proc", "sys")
_PATH = re.compile(r"(?<![A-Za-z0-9:])/(?:data|root|home|tmp|var|etc|srv|opt|usr|run|proc|sys)(?:/[^\s<>\"'()\[\]`,;]*)?")


class VisibleText:
    def __init__(self):
        self.pending = ""

    def feed(self, piece: str, final: bool = False) -> str:
        self.pending += piece
        if final:
            text, self.pending = self.pending, ""
        else:
            suffix = re.search(r"[^\s<>\"'()\[\]`,;]*\Z", self.pending)
            cut = len(self.pending)
            token = suffix.group() if suffix else ""
            slash = token.find("/")
            if slash >= 0:
                candidate = token[slash + 1:]
                if any(root.startswith(candidate) or candidate.startswith(root + "/") or candidate == root
                       for root in _ROOTS):
                    cut = suffix.start() + slash
            text, self.pending = self.pending[:cut], self.pending[cut:]
            if len(self.pending) > 4096:
                text += "[过长片段已省略]"
                self.pending = ""
        return _PATH.sub("[本地路径]", text)


@dataclass
class _Turn:
    thread_id: str
    turn_id: str
    trace_id: str
    owner: str
    started: dict
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    seq: int = 0
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    cancelled: bool = False
    finishing: bool = False
    reset_requested: bool = False
    final: dict | None = None
    tool_calls: dict[str, dict] = field(default_factory=dict)
    tools_truncated: bool = False


class _OutputLimit(Exception):
    pass


class WebChatService:
    def __init__(self, store: WebChatStore, agent, sessions, registry, dream):
        self.store = store
        self.agent = agent
        self.sessions = sessions
        self.registry = registry
        self.dream = dream
        self.notifications_enabled = False
        self.queue = SessionMessageQueue(max_pending=1)
        self._active: dict[str, _Turn] = {}
        self._admissions: dict[asyncio.Task, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def active_count(self) -> int:
        return len(self._active)

    @staticmethod
    def session_key(thread_id: str) -> str:
        return f"web:{thread_id}"

    async def reset(self, thread_id: str) -> dict:
        async with self._lock:
            if thread_id in self._active:
                raise ChatError("busy", "请先停止当前回复", 409)
            await asyncio.to_thread(self.store.get_thread, thread_id)
            key = self.session_key(thread_id)
            self.agent.reset_session(key)
            self.sessions.reset_session(key)
            return await asyncio.to_thread(self.store.reset_thread, thread_id)

    async def delete(self, thread_id: str) -> dict:
        async with self._lock:
            await self.stop(thread_id)
            await asyncio.to_thread(self.store.get_thread, thread_id)
            reminders, daily = getattr(self, "reminders", None), getattr(self, "daily", None)
            if reminders:
                for reminder in reminders.list_reminders():
                    if reminder.channel == "web" and reminder.chat_id == thread_id:
                        await reminders.cancel(reminder.reminder_id)
            if daily:
                for task in daily.list_tasks():
                    if task.channel == "web" and task.target_id == thread_id:
                        await daily.cancel(task.task_id)
            key = self.session_key(thread_id)
            self.agent.retire_session(key)
            self.sessions.reset_session(key)
            return await asyncio.to_thread(self.store.delete_thread, thread_id)

    def _cancel(self, entry: _Turn) -> bool:
        if entry.cancelled or entry.finishing or entry.completed.is_set():
            return False
        entry.cancelled = True
        self.agent.cancel(entry.trace_id)
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
        return True

    async def stop(self, thread_id: str, *, entry: _Turn | None = None) -> bool:
        active = self._active.get(thread_id)
        if active is None or (entry is not None and active is not entry):
            return False
        key = self.session_key(thread_id)
        self._cancel(active)
        self.registry.cancel(key)
        if active.task is None:
            await self.queue.clear(key)
            await self._finish(active, "", "cancelled")
        else:
            await active.completed.wait()
        return True

    async def cancel_turn(self, thread_id: str, client_turn_id: str, owner: str) -> bool:
        entry = self._active.get(thread_id)
        if entry is None or entry.owner != owner or entry.started["turn"]["client_turn_id"] != client_turn_id:
            return False
        return await self.stop(thread_id, entry=entry)

    async def cancel_all(self, owner: str | None = None) -> None:
        pending = [task for task, task_owner in list(self._admissions.items()) if owner is None or owner == task_owner]
        if pending:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        entries = [entry for entry in list(self._active.values()) if owner is None or owner == entry.owner]
        await asyncio.gather(*(self.stop(entry.thread_id, entry=entry) for entry in entries))

    async def close(self) -> None:
        self._closed = True
        if self._admissions:
            await asyncio.gather(*list(self._admissions), return_exceptions=True)
        await self.cancel_all()
        await self.queue.close()

    async def open_turn(self, thread_id: str, data, owner: str) -> AsyncIterator[dict]:
        if self._closed:
            raise ChatError("unavailable", "聊天服务暂不可用", 503)
        # A disconnected POST must not abandon a ledger reservation halfway through.
        task = asyncio.create_task(self._open_turn(thread_id, data, owner))
        self._admissions[task] = owner

        def settled(task: asyncio.Task) -> None:
            self._admissions.pop(task, None)
            if not task.cancelled():
                task.exception()  # Retrieve errors even if the HTTP caller has gone away.

        task.add_done_callback(settled)
        return await asyncio.shield(task)

    async def _open_turn(self, thread_id: str, data, owner: str) -> AsyncIterator[dict]:
        async with self._lock:
            if self._closed:
                raise ChatError("unavailable", "聊天服务暂不可用", 503)
            existing = await asyncio.to_thread(
                self.store.lookup_turn, thread_id, data.client_turn_id, data.text,
                data.attachment_ids, data.retry_of,
            )
            if existing is not None:
                return await self.subscribe(thread_id, existing["turn"]["id"], owner)
            if data.text.strip().lower() == "/stop":
                await self.stop(thread_id)
            if self._active:
                raise ChatError("busy", "助手正在回复，请稍后发送或先停止", 409)
            started = await asyncio.to_thread(
                self.store.begin_turn, thread_id, data.client_turn_id, data.text,
                data.attachment_ids, data.retry_of,
            )
            if started["duplicate"]:
                return self._replay(thread_id, started)
            entry = _Turn(thread_id, started["turn"]["id"], uuid.uuid4().hex, owner, started)
            self._active[thread_id] = entry
            key = self.session_key(thread_id)

            async def job():
                entry.task = asyncio.current_task()
                if entry.cancelled:
                    await self._finish(entry, "", "cancelled")
                    return
                await self._run_turn(entry, session_key=key)

            accepted = await self.queue.submit(key, job, queue_if_busy=False)
            if not accepted.accepted:
                await self._finish(entry, "", "error")
                raise ChatError("busy", "当前对话正在回复", 409)
            return self._stream(entry)

    async def active(self, thread_id: str) -> dict:
        self.store.validate_id(thread_id)
        # Do not expose the short gap between durable admission and task creation.
        async with self._lock:
            entry = self._active.get(thread_id)
            if entry is not None:
                return self._active_snapshot(entry)
            latest = await asyncio.to_thread(self.store.get_turn, thread_id)
            return {"active": False, "turn": latest["turn"] if latest else None,
                    "user_message": latest["user"] if latest else None,
                    "assistant_message": latest["assistant"] if latest else None,
                    "seq": latest["seq"] if latest else 0}

    @staticmethod
    def _active_snapshot(entry: _Turn) -> dict:
        return copy.deepcopy({"active": not entry.completed.is_set(), "turn": entry.started["turn"],
                              "user_message": entry.started["user"], "assistant_message": entry.started["assistant"],
                              "seq": entry.seq})

    async def subscribe(self, thread_id: str, turn_id: str, owner: str) -> AsyncIterator[dict]:
        self.store.validate_id(thread_id)
        self.store.validate_id(turn_id)
        entry = self._active.get(thread_id)
        if entry is not None and entry.turn_id == turn_id:
            if entry.owner != owner:
                raise ChatError("forbidden", "不能订阅其他登录会话的运行轮次", 403)
            return self._stream(entry)
        started = await asyncio.to_thread(self.store.get_turn, thread_id, turn_id)
        if started["turn"]["status"] == "running":
            raise ChatError("unavailable", "轮次状态正在恢复，请稍后重试", 503)
        return self._replay(thread_id, started)

    @staticmethod
    def _accepted(entry: _Turn) -> dict:
        return copy.deepcopy({"v": 1, "type": "accepted", "thread_id": entry.thread_id, "turn_id": entry.turn_id,
                              "seq": entry.seq, "user_message": entry.started["user"],
                              "assistant_message": entry.started["assistant"]})

    def _publish(self, entry: _Turn, event: dict) -> None:
        """Snapshot and fan-out are synchronous: consumers never backpressure pi."""
        if entry.finishing or entry.completed.is_set():
            return
        entry.seq += 1
        message = entry.started["assistant"]
        if event["type"] == "delta":
            message["text"] += event["text"]
        if "tool_calls" in event:
            message["tool_calls"] = copy.deepcopy(event["tool_calls"])
            message["tools_truncated"] = event["tools_truncated"]
        message["updated_at"] = time.time()
        for queue in entry.subscribers:
            if queue.full():
                # Rebase using an already supported event, not a new snapshot type.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self._accepted(entry))
            else:
                queue.put_nowait(copy.deepcopy({"v": 1, "turn_id": entry.turn_id, "seq": entry.seq, **event}))

    async def _replay(self, thread_id: str, started: dict):
        yield {"v": 1, "type": "accepted", "thread_id": thread_id, "turn_id": started["turn"]["id"],
               "seq": started.get("seq", 0), "user_message": started["user"], "assistant_message": started["assistant"]}
        yield {"v": 1, "type": "done", "turn_id": started["turn"]["id"], "seq": started.get("seq", 0),
               "status": started["assistant"]["status"], "message": started["assistant"]}

    def _stream(self, entry: _Turn) -> AsyncIterator[dict]:
        if len(entry.subscribers) >= 16:
            raise ChatError("busy", "当前轮次订阅过多", 409)
        queue = asyncio.Queue(maxsize=32)
        entry.subscribers.add(queue)
        return self._subscription(entry, queue, self._accepted(entry))

    async def _subscription(self, entry: _Turn, queue: asyncio.Queue, accepted: dict):
        try:
            yield accepted
            while not entry.completed.is_set() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if event is not None:
                        yield event
                except asyncio.TimeoutError:
                    if not entry.completed.is_set():
                        yield {"v": 1, "type": "heartbeat", "turn_id": entry.turn_id}
            if entry.final:
                yield copy.deepcopy(entry.final)
        finally:
            entry.subscribers.discard(queue)

    async def _finish(self, entry: _Turn, text: str, status: str) -> None:
        if entry.finishing:
            await entry.completed.wait()
            return
        entry.finishing = True
        text = VisibleText().feed(text, final=True)
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            text, status = "回复超出长度限制，已停止。", "truncated"
        for tool in entry.tool_calls.values():
            if tool["status"] == "running":
                tool["status"] = "cancelled" if status == "cancelled" else "interrupted"
                tool["duration_seconds"] = max(0.0, time.time() - tool["started_at"])
        notification = None
        if self.notifications_enabled and status == "complete" and not entry.started["user"]["text"].lstrip().startswith("/"):
            notification = {"event_id": entry.turn_id, "thread_id": entry.thread_id,
                            "message_id": entry.started["assistant"]["id"], "kind": "reply", "owner": entry.owner}
        entry.seq += 1
        try:
            message = await asyncio.to_thread(self.store.finish_turn, entry.thread_id, entry.turn_id, text, status,
                                              list(entry.tool_calls.values()), entry.tools_truncated,
                                              notification=notification, seq=entry.seq)
            entry.started["assistant"] = message
            entry.started["turn"].update(status=message["status"], updated_at=message["updated_at"])
            if entry.reset_requested and status == "complete":
                await asyncio.to_thread(self.store.reset_thread, entry.thread_id)
            entry.final = {"v": 1, "type": "done", "turn_id": entry.turn_id, "seq": entry.seq,
                           "status": message["status"], "message": message}
        except Exception as exc:
            logger.warning("web turn persistence failed", extra={"event": "web.store_error", "error_code": type(exc).__name__})
            entry.final = {"v": 1, "type": "error", "turn_id": entry.turn_id, "code": "unavailable",
                           "message": "回复状态暂不可用，请重新加载对话"}
        finally:
            if self._active.get(entry.thread_id) is entry:
                self._active.pop(entry.thread_id, None)
            self.registry.finish(self.session_key(entry.thread_id), entry.trace_id)
            entry.completed.set()
            for queue in entry.subscribers:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(None)
            entry.subscribers.clear()

    @observe_run(channel="web", source="interactive")
    async def _run_turn(self, entry: _Turn, *, session_key: str) -> None:
        text = ""
        status = "error"
        if not self.registry.start(session_key, entry.trace_id, entry.started["user"]["id"],
                                   lambda: self._cancel(entry)):
            await self._finish(entry, "当前对话正在运行，请稍后重试。", "error")
            return
        emit("message", channel="web", source="interactive", status="received", count=1)
        try:
            text = await asyncio.wait_for(self._execute(entry, session_key), timeout=_TURN_TIMEOUT)
            status = "complete"
            set_status("success")
        except (asyncio.CancelledError, AgentClientCancelled):
            text = "回复已停止。"
            status = "cancelled"
            set_status("cancelled")
        except _OutputLimit:
            text = "回复超出长度限制，已停止；请缩小问题范围后重试。"
            status = "truncated"
            set_status("error")
        except asyncio.TimeoutError:
            self.agent.cancel(entry.trace_id)
            text = "回复超时，请稍后重试。"
            set_status("error")
        except (AgentClientError, ChatError):
            text = "助手暂时无法完成回复，请稍后重试。"
            set_status("error")
        except Exception as exc:
            logger.warning("web turn failed", extra={"event": "web.turn_error", "error_code": type(exc).__name__})
            text = "回复未完成，请稍后重试。"
            set_status("error")
        finally:
            await self._finish(entry, text, status)

    async def _execute(self, entry: _Turn, key: str) -> str:
        user = entry.started["user"]
        text = user["text"].strip()
        if text.startswith("/"):
            return await self._command(entry, key, text)
        paths, notices = [], []
        for attachment in user.get("attachments", []):
            path, meta = await asyncio.to_thread(self.store.attachment, entry.thread_id, attachment["id"])
            if meta["kind"] == "image":
                paths.append(str(path))
            else:
                notices.append(format_file_notice(meta["name"], str(path), meta["size"]))
        prompt = apply_pending_notices(text or "请查看我上传的附件。", notices)
        self._publish(entry, {"type": "progress", "stage": "working", "label": "正在思考…"})

        async def progress(label):
            if entry.tool_calls:
                return
            self._publish(entry, {"type": "progress", "stage": "tool" if label else "working",
                                  "label": "正在使用工具…" if label else "正在继续回复…"})

        async def tool_activity(event):
            call_id = str(event.get("call_id", ""))[:256]
            phase = event.get("phase")
            if not call_id or phase not in {"start", "end", "interrupted"}:
                return
            tool = entry.tool_calls.get(call_id)
            if tool is None:
                if len(entry.tool_calls) >= 30:
                    if not entry.tools_truncated:
                        entry.tools_truncated = True
                        self._publish(entry, {"type": "progress", "stage": "tool", "label": "工具活动已达显示上限",
                                              "message_id": entry.started["assistant"]["id"],
                                              "tool_calls": list(entry.tool_calls.values()), "tools_truncated": True})
                    return
                name = str(event.get("name", "other"))
                name = name if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", name) else "other"
                summary = sanitize_tool_summary(str(event.get("summary", "调用工具"))) or "调用工具"
                tool = {"id": uuid.uuid4().hex, "name": name, "summary": summary, "status": "running",
                        "started_at": time.time(), "duration_seconds": None}
                entry.tool_calls[call_id] = tool
            if phase == "end":
                tool["status"] = "error" if event.get("is_error") else "completed"
            elif phase == "interrupted":
                tool["status"] = "cancelled" if entry.cancelled else "interrupted"
            if phase != "start":
                duration = event.get("duration_seconds")
                tool["duration_seconds"] = max(0.0, min(86400.0, duration)) if type(duration) in (int, float) else None
            active = [item for item in entry.tool_calls.values() if item["status"] == "running"]
            label = f"正在{active[-1]['summary']}…" if active else "工具调用结束，正在继续回复…"
            if not entry.cancelled and not entry.completed.is_set():
                self._publish(entry, {"type": "progress", "stage": "tool", "label": label,
                                      "message_id": entry.started["assistant"]["id"],
                                      "tool_calls": [dict(item) for item in entry.tool_calls.values()],
                                      "tools_truncated": entry.tools_truncated})

        visible = VisibleText()
        parts = []
        size = 0
        final_text: str | None = None

        async def on_final_text(text: str) -> None:
            nonlocal final_text
            if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise _OutputLimit()
            final_text = VisibleText().feed(text, final=True)

        stream = self.agent.chat_stream(messages=[{"role": "user", "content": prompt}], trace_id=entry.trace_id,
                                        session_key=key, image_paths=paths, on_progress=progress,
                                        on_tool_event=tool_activity, on_final_text=on_final_text)
        async with contextlib.aclosing(stream):
            async for piece in stream:
                size += len(piece.encode("utf-8"))
                if size > MAX_OUTPUT_BYTES:
                    self.agent.cancel(entry.trace_id)
                    raise _OutputLimit()
                chunk = visible.feed(piece)
                if chunk:
                    parts.append(chunk)
                    self._publish(entry, {"type": "delta", "message_id": entry.started["assistant"]["id"], "text": chunk})
        last = visible.feed("", final=True)
        if last:
            parts.append(last)
            self._publish(entry, {"type": "delta", "message_id": entry.started["assistant"]["id"], "text": last})
        return final_text if final_text is not None else ("".join(parts) or "(空响应)")

    async def _command(self, entry: _Turn, key: str, text: str) -> str:
        words = text.lower().split()
        command = words[0]
        catalog = command_catalog("web")
        supported = {name for spec in catalog for name in (spec["command"], *spec["aliases"])}
        if command not in supported:
            return "PWA 暂不支持这个命令。输入 / 查看可用命令。"
        if command in {"/remind", "/timer"}:
            reminder = parse_reminder_command(text)
            if reminder is None:
                return "用法：/remind 10m 提醒内容（支持 s/m/h/d）。"
            scheduler = getattr(self, "reminders", None)
            if scheduler is None:
                return "提醒服务未配置。"
            scheduled = await scheduler.schedule(entry.thread_id, reminder.text, reminder.delay_seconds, channel="web")
            return f"已设置提醒 {scheduled.reminder_id[:8]}，到时显示在本会话；已开启通知的设备会收到提醒。"
        if command == "/daily":
            scheduler = getattr(self, "daily", None)
            if scheduler is None:
                return "每日任务服务未配置。"
            return await execute_daily_command(parse_daily_command(text), scheduler, "web", entry.thread_id)
        if command == "/help" and len(words) == 1:
            return build_help_text(channel="web")
        if command == "/skills" and len(words) == 1:
            return await asyncio.to_thread(build_skill_summary)
        if command == "/new" and len(words) == 1:
            thread = await asyncio.to_thread(self.store.create_thread)
            self._publish(entry, {"type": "thread_created", "thread": thread})
            return "已创建新对话，不继承当前上下文。"
        if command == "/reset" and len(words) == 1:
            self.agent.reset_session(key)
            self.sessions.reset_session(key)
            entry.reset_requested = True
            return "当前上下文已重置；历史记录仍保留。"
        if command == "/stop" and len(words) == 1:
            return "已停止当前回复。"
        action = parse_memory_command(text)
        if action == "status":
            return await asyncio.to_thread(self.dream.status)
        if action == "undo":
            return await self.dream.undo()
        if action in ("dream", "dream_all"):
            if not self.dream.enabled:
                return "Dream 未启用。"
            self._publish(entry, {"type": "progress", "stage": "dream", "label": "正在整理记忆…"})
            result = await self.dream.run("manual", session_key=key if action == "dream" else None, deliver=False)
            return result.message
        if command in ("/backend", "/pi") and len(words) == 1:
            return process_command(text, self.sessions, key, self.agent).reply_text
        return "命令参数无效。输入 / 查看用法。"
