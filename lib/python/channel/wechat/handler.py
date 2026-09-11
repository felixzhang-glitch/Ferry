from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.commands import (
    build_help_text,
    execute_daily_command,
    parse_daily_command,
    parse_reminder_command,
    process_command,
)
from app.config import Settings
from app.skills import build_skill_summary
from channel.feishu.formatting import normalize_reply_text, split_message_text
from core.agent.types import AgentClient, AgentClientCancelled
from core.session.daily_scheduler import DailyTaskScheduler
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import SessionManager, apply_pending_notices, format_file_notice
from core.session.message_queue import SessionMessageQueue
from core.session.task_registry import ActiveTaskRegistry

logger = logging.getLogger(__name__)

WECHAT_HELP_TEXT = build_help_text(include_remind=False)


@dataclass(slots=True)
class WeChatFileAttachment:
    name: str
    path: str
    size: int = 0


@dataclass(slots=True)
class WeChatTextMessageEvent:
    message_id: str
    account_id: str
    user_id: str
    text: str
    context_token: str = ""
    files: tuple[WeChatFileAttachment, ...] = ()


class WeChatWebhookHandler:
    def __init__(
        self,
        settings: Settings,
        agent_client: AgentClient,
        session_manager: SessionManager,
        deduplicator: MessageDeduplicator,
        task_registry: ActiveTaskRegistry,
        daily_scheduler: DailyTaskScheduler | None = None,
        message_queue: SessionMessageQueue | None = None,
    ) -> None:
        self._settings = settings
        self._agent_client = agent_client
        self._sessions = session_manager
        self._deduplicator = deduplicator
        self._task_registry = task_registry
        self._daily_scheduler = daily_scheduler
        self._message_queue = message_queue or SessionMessageQueue(max_pending=3)

    async def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> dict[str, Any]:
        self._verify_token(headers)
        payload = self._parse_payload(raw_body)
        event = self._parse_event(payload)
        trace_id = uuid.uuid4().hex

        replies = await self._handle_text_event(event=event, trace_id=trace_id)
        return {
            "code": 0,
            "replies": replies,
            "context_token": event.context_token,
        }

    async def _handle_text_event(self, event: WeChatTextMessageEvent, trace_id: str) -> list[str]:
        if self._deduplicator.seen(f"wechat:{event.message_id}"):
            logger.info("duplicate wechat message ignored", extra={"trace_id": trace_id, "event": "wechat.deduplicate"})
            return []

        session_key = self._build_session_key(event)
        normalized_text = event.text.strip().lower()

        for attachment in event.files:
            self._sessions.note_incoming_file(
                session_key,
                format_file_notice(attachment.name, attachment.path, attachment.size),
            )
        if event.files:
            logger.info(
                "wechat inbound files queued",
                extra={"trace_id": trace_id, "event": "wechat.file_archive", "count": len(event.files)},
            )
            # The sidecar already replied 已收藏, and a file-only message must not
            # wake the agent or it will read the file unprompted. When text
            # accompanies the files, fall through and let the notices ride it.
            if not event.text:
                return []

        if normalized_text == "/stop":
            dropped = await self._message_queue.clear(session_key)
            task = self._task_registry.cancel(session_key)
            if task is None and dropped == 0:
                return self._split_reply("当前没有可终止的运行中任务。")
            parts = []
            if task is not None:
                parts.append("已收到停止请求，正在强制终止当前任务。")
            if dropped > 0:
                parts.append(f"已清空排队消息 {dropped} 条。")
            return self._split_reply(" ".join(parts))

        if normalized_text == "/help":
            return self._split_reply(WECHAT_HELP_TEXT)

        daily = parse_daily_command(event.text)
        if daily is not None:
            if self._daily_scheduler is None:
                return self._split_reply("每日简报组件未启用。")
            reply = await execute_daily_command(
                daily=daily,
                scheduler=self._daily_scheduler,
                channel="wechat",
                target_id=event.user_id,
            )
            return self._split_reply(reply)

        reminder = parse_reminder_command(event.text)
        if reminder is not None:
            return self._split_reply("微信渠道暂不支持定时提醒，请通过飞书使用 /remind 命令。")

        command = process_command(
            event.text,
            session_manager=self._sessions,
            session_key=session_key,
            agent_client=self._agent_client,
        )
        if command is not None:
            return self._split_reply(command.reply_text)

        if normalized_text == "/skills":
            skills = await asyncio.to_thread(build_skill_summary)
            if not skills:
                return self._split_reply("当前本机未发现可用 skills。")
            return self._split_reply(f"当前本机可用 skills:\n{skills}")

        result_future: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()

        async def job() -> None:
            replies = await self._run_llm_job(event=event, session_key=session_key, trace_id=trace_id)
            if not result_future.done():
                result_future.set_result(replies)

        def on_drop() -> None:
            if not result_future.done():
                result_future.set_result(self._split_reply("该消息已被 /stop 清出队列，未处理。"))

        status = await self._message_queue.submit(session_key, job, on_drop=on_drop)
        if not status.accepted:
            return self._split_reply("当前会话排队消息已满，请稍后再发，或发送 /stop 终止当前任务并清空队列。")
        # Hold the webhook request until the queued job completes so the reply
        # maps to this message (sidecar timeout applies to queue wait + run).
        try:
            return await result_future
        except asyncio.CancelledError:
            if not result_future.done():
                result_future.cancel()
            raise

    async def _run_llm_job(self, event: WeChatTextMessageEvent, session_key: str, trace_id: str) -> list[str]:
        started = self._task_registry.start(
            key=session_key,
            trace_id=trace_id,
            message_id=event.message_id,
            cancel_callback=lambda: self._agent_client.cancel(trace_id),
        )
        if not started:
            return self._split_reply("当前已有任务在运行中。发送 /stop 可强制终止后再试。")

        # Drain only once the turn is committed, so a rejected turn keeps the
        # notices queued for the next one.
        user_text = apply_pending_notices(event.text, self._sessions.take_pending_files(session_key))
        messages = [{"role": "user", "content": user_text}]

        try:
            if self._settings.streaming_enabled:
                parts: list[str] = []
                async for piece in self._agent_client.chat_stream(
                    messages=messages, trace_id=trace_id, session_key=session_key
                ):
                    parts.append(piece)
                answer = "".join(parts).strip()
            else:
                answer = await self._agent_client.chat(
                    messages=messages, trace_id=trace_id, session_key=session_key
                )

            if not answer.strip():
                answer = "(空响应)"
            return self._split_reply(answer)
        except AgentClientCancelled:
            logger.info("wechat message cancelled by user", extra={"trace_id": trace_id, "event": "wechat.cancel"})
            return self._split_reply("当前任务已终止。")
        except Exception:
            logger.exception("failed to process wechat message", extra={"trace_id": trace_id, "event": "wechat.error"})
            return self._split_reply("服务繁忙，请稍后重试。")
        finally:
            self._task_registry.finish(key=session_key, trace_id=trace_id)

    def _verify_token(self, headers: Mapping[str, str]) -> None:
        expected = self._settings.wechat_webhook_token.strip()
        if not expected:
            return

        auth_header = ""
        for key, value in headers.items():
            if key.lower() == "authorization":
                auth_header = value.strip()
                break
        if auth_header != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="invalid wechat webhook token")

    @staticmethod
    def _parse_payload(raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid request body") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload type")
        return payload

    @staticmethod
    def _parse_files(payload: dict[str, Any]) -> tuple[WeChatFileAttachment, ...]:
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            return ()

        attachments: list[WeChatFileAttachment] = []
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            try:
                size = max(0, int(entry.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            attachments.append(
                WeChatFileAttachment(name=str(entry.get("name") or "").strip(), path=path, size=size)
            )
        return tuple(attachments)

    @staticmethod
    def _parse_event(payload: dict[str, Any]) -> WeChatTextMessageEvent:
        message_id = str(payload.get("message_id", "") or payload.get("client_id", "")).strip()
        account_id = str(payload.get("account_id", "")).strip()
        user_id = str(payload.get("user_id", "") or payload.get("from_user_id", "")).strip()
        text = str(payload.get("text", "")).strip()
        context_token = str(payload.get("context_token", "")).strip()
        files = WeChatWebhookHandler._parse_files(payload)

        if not message_id or not account_id or not user_id:
            raise HTTPException(status_code=400, detail="missing wechat message identity")
        if not text and not files:
            raise HTTPException(status_code=400, detail="missing wechat text")

        return WeChatTextMessageEvent(
            message_id=message_id,
            account_id=account_id,
            user_id=user_id,
            text=text,
            context_token=context_token,
            files=files,
        )

    @staticmethod
    def _build_session_key(event: WeChatTextMessageEvent) -> str:
        return f"wechat:{event.account_id}:{event.user_id}"

    def _split_reply(self, text: str) -> list[str]:
        content = normalize_reply_text(text) or "(空响应)"
        max_chars = int(getattr(self._settings, "wechat_message_chunk_chars", 1800) or 1800)
        return split_message_text(content, max_chars=max_chars)
