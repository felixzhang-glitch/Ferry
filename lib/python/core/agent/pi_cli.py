from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app import memory
from app.clock import time_context
from app.config import Settings
from app.skills import build_skill_summary
from core.agent.types import AgentClientCancelled, AgentClientError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


class PiCliClient:
    """Backend client for the `pi` coding agent (`pi --mode json`).

    Owns native session IDs, subprocess lifecycle, retries and event parsing.
    Channels use this client directly; conversation history stays in pi.

    * `message_update.assistantMessageEvent.text_delta` carries real deltas.
    * `pi --mode json` always exits 0, even when the provider rejects the
      request. Success has to be decided from the event stream: the assistant
      `message_end` carries `stopReason` and, on failure, `errorMessage`.

    Sessions are addressed by an ID we generate ourselves and pass through
    `--session-id` (pi creates the session when the ID is unknown), so nothing
    has to be parsed out of the stream to keep a conversation going.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        bin_path: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self._settings = settings
        self._name = "pi"
        self._bin = bin_path or settings.pi_cli_bin
        self._model = (model if model is not None else settings.pi_model).strip()
        self._thinking = settings.pi_thinking.strip()
        self._tools = settings.pi_tools.strip()
        self._agent_dir = settings.pi_agent_dir.strip()
        self._api_key = settings.pi_api_key.strip()
        self._offline = settings.pi_offline
        self._approve_project = settings.pi_approve_project
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.pi_timeout_seconds
        )
        self._idle_timeout_seconds = (
            idle_timeout_seconds
            if idle_timeout_seconds is not None
            else settings.pi_idle_timeout_seconds
        )

        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._stream_piece_chars = 80
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requests: set[str] = set()

        self._work_dir = os.path.abspath(os.path.expanduser(settings.pi_work_dir))
        os.makedirs(self._work_dir, exist_ok=True)

        self._session_store_path = os.path.abspath(settings.pi_session_store_path)
        self._session_lock = threading.Lock()
        self._session_ids: dict[str, str] = self._load_sessions()

    @property
    def name(self) -> str:
        return self._name

    async def close(self) -> None:
        with self._process_lock:
            processes = list(self._active_processes.items())
            self._cancel_requests.update(trace_id for trace_id, _ in processes)
        await asyncio.gather(*(self._terminate_process(process) for _, process in processes))

    def reset_session(self, session_key: str) -> None:
        with self._session_lock:
            if session_key in self._session_ids:
                del self._session_ids[session_key]
                self._save_sessions()

    async def chat(
        self,
        messages: list[dict[str, str]],
        trace_id: str,
        *,
        session_key: str | None = None,
        image_paths: list[str] | None = None,
    ) -> str:
        self._assert_circuit_closed()

        session_id, is_new = self._get_or_create_session_id(session_key)
        session_holder: dict[str, str] = {}
        prompt = self._build_native_prompt(messages, include_preamble=is_new)
        request_summary = self._summarize_messages(messages)
        resolved_images = self._resolve_image_paths(image_paths)
        attempt = 0
        start = time.monotonic()

        try:
            while True:
                try:
                    text = await self._run_once(
                        prompt=prompt,
                        trace_id=trace_id,
                        session_id=session_id,
                        session_holder=session_holder,
                        image_paths=resolved_images,
                    )
                    self._record_success()
                    self._persist_session(session_key, session_holder, expected_id=session_id)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "pi cli chat completed",
                        extra={
                            "trace_id": trace_id,
                            "event": "pi.chat",
                            "backend": self._name,
                            "duration_ms": duration_ms,
                            "status_code": 0,
                            "request_summary": request_summary,
                            "response_summary": self._truncate(text),
                        },
                    )
                    return text
                except AgentClientCancelled:
                    logger.info(
                        "pi cli chat cancelled",
                        extra={"trace_id": trace_id, "event": "pi.chat", "status_code": 499},
                    )
                    raise
                except AgentClientError as exc:
                    if not self._should_retry(exc) or attempt >= self._settings.pi_max_retries:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "pi cli chat failed",
                            extra={
                                "trace_id": trace_id,
                                "event": "pi.chat",
                                "backend": self._name,
                                "duration_ms": duration_ms,
                                "status_code": 1,
                                "error_code": type(exc).__name__,
                                "request_summary": request_summary,
                            },
                        )
                        raise
                    self._raise_if_cancelled(trace_id)
                    await asyncio.sleep(self._settings.pi_retry_backoff_seconds * (2**attempt))
                    self._raise_if_cancelled(trace_id)
                    attempt += 1
        finally:
            self._clear_cancel_request(trace_id)

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        trace_id: str,
        *,
        session_key: str | None = None,
        image_paths: list[str] | None = None,
    ) -> AsyncIterator[str]:
        self._assert_circuit_closed()

        session_id, is_new = self._get_or_create_session_id(session_key)
        session_holder: dict[str, str] = {}
        prompt = self._build_native_prompt(messages, include_preamble=is_new)
        request_summary = self._summarize_messages(messages)
        resolved_images = self._resolve_image_paths(image_paths)
        attempt = 0
        start = time.monotonic()

        try:
            while True:
                emitted = False
                preview_parts: list[str] = []
                try:
                    async for piece in self._run_stream_once(
                        prompt=prompt,
                        trace_id=trace_id,
                        session_id=session_id,
                        session_holder=session_holder,
                        image_paths=resolved_images,
                    ):
                        emitted = True
                        if len("".join(preview_parts)) < 240:
                            preview_parts.append(piece)
                        yield piece

                    self._record_success()
                    self._persist_session(session_key, session_holder, expected_id=session_id)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "pi cli stream completed",
                        extra={
                            "trace_id": trace_id,
                            "event": "pi.stream",
                            "backend": self._name,
                            "duration_ms": duration_ms,
                            "status_code": 0,
                            "request_summary": request_summary,
                            "response_summary": self._truncate("".join(preview_parts)),
                        },
                    )
                    return
                except AgentClientCancelled:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "pi cli stream cancelled",
                        extra={
                            "trace_id": trace_id,
                            "event": "pi.stream",
                            "backend": self._name,
                            "duration_ms": duration_ms,
                            "status_code": 499,
                            "request_summary": request_summary,
                            "response_summary": self._truncate("".join(preview_parts)),
                        },
                    )
                    raise
                except AgentClientError as exc:
                    if emitted:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "pi cli stream interrupted after partial output",
                            extra={
                                "trace_id": trace_id,
                                "event": "pi.stream",
                                "backend": self._name,
                                "duration_ms": duration_ms,
                                "status_code": 1,
                                "error_code": type(exc).__name__,
                                "request_summary": request_summary,
                                "response_summary": self._truncate("".join(preview_parts)),
                            },
                        )
                        raise

                    if not self._should_retry(exc) or attempt >= self._settings.pi_max_retries:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "pi cli stream failed",
                            extra={
                                "trace_id": trace_id,
                                "event": "pi.stream",
                                "backend": self._name,
                                "duration_ms": duration_ms,
                                "status_code": 1,
                                "error_code": type(exc).__name__,
                                "request_summary": request_summary,
                            },
                        )
                        raise

                    self._raise_if_cancelled(trace_id)
                    await asyncio.sleep(self._settings.pi_retry_backoff_seconds * (2**attempt))
                    self._raise_if_cancelled(trace_id)
                    attempt += 1
        finally:
            self._clear_cancel_request(trace_id)

    def cancel(self, trace_id: str) -> bool:
        with self._process_lock:
            process = self._active_processes.get(trace_id)
            if process is None:
                return False
            self._cancel_requests.add(trace_id)

        if process.returncode is None:
            self._kill_process_group(process)
        return True

    async def _run_stream_once(
        self,
        prompt: str,
        trace_id: str,
        *,
        session_id: str | None = None,
        session_holder: dict[str, str] | None = None,
        image_paths: list[str] | None = None,
    ) -> AsyncIterator[str]:
        command = self._build_command(session_id=session_id, image_paths=image_paths)
        command.append(prompt)
        process = await self._spawn_process(command)
        self._register_process(trace_id, process)
        stderr_task = asyncio.create_task(self._read_stream_text(process.stderr))
        deadline = time.monotonic() + self._timeout_seconds

        error_messages: list[str] = []
        fallback_parts: list[str] = []
        final_text = ""
        # Decided by the *last* assistant message_end: pi can fail an attempt,
        # auto-retry and then succeed, so an intermediate error is not the
        # verdict for the turn.
        final_stop_reason = ""
        final_error = ""
        emitted_text = False

        try:
            while True:
                self._raise_if_cancelled(trace_id)
                line = await asyncio.wait_for(
                    self._readline_with_idle_timeout(process.stdout),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip("\r\n").strip()
                if not text:
                    continue

                event = self._parse_event(text)
                if event is None:
                    fallback_parts.append(text)
                    continue

                if session_holder is not None and "id" not in session_holder:
                    found = self._extract_session_id(event)
                    if found:
                        session_holder["id"] = found

                message = self._assistant_message(event)
                if message is not None:
                    final_stop_reason = str(message.get("stopReason", ""))
                    final_error = self._stop_reason_error(message)
                    if final_error:
                        error_messages.append(final_error)
                    assistant_text = self._extract_final_text(event)
                    if assistant_text:
                        final_text = assistant_text
                else:
                    error_message = self._extract_error_message(event)
                    if error_message:
                        error_messages.append(error_message)

                delta = self._extract_text_delta(event)
                if delta:
                    emitted_text = True
                    for chunk in self._split_chunks(delta):
                        yield chunk
            return_code = await asyncio.wait_for(
                process.wait(), timeout=max(0.0, deadline - time.monotonic())
            )
            stderr_text = await asyncio.wait_for(
                asyncio.shield(stderr_task), timeout=max(0.0, deadline - time.monotonic())
            )
        except asyncio.TimeoutError as exc:
            self._raise_if_cancelled(trace_id)
            raise AgentClientError(f"{self._name} cli timeout") from exc
        finally:
            # Also reap the process on task cancellation or a closed stream.
            try:
                await self._terminate_process(process)
            finally:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                self._unregister_process(trace_id)

        self._raise_if_cancelled(trace_id)
        if return_code != 0:
            error_hint = (
                " | ".join(error_messages).strip()
                or "\n".join(fallback_parts).strip()
                or stderr_text.strip()
            )
            raise AgentClientError(
                f"{self._name} cli failed: return_code={return_code}, error={self._truncate(error_hint)}"
            )

        # `pi --mode json` exits 0 even on provider/auth errors, so the event
        # stream is the only reliable signal. The turn's verdict is the last
        # assistant `message_end`: failing it after streaming partial text still
        # counts as a failure, otherwise a truncated answer would be delivered
        # as if it were complete.
        if final_stop_reason in {"error", "aborted"}:
            detail = final_error or " | ".join(error_messages) or final_stop_reason
            raise AgentClientError(f"{self._name} cli error: {self._truncate(detail)}")

        if error_messages and not emitted_text and not final_text:
            raise AgentClientError(f"{self._name} cli error: {self._truncate(' | '.join(error_messages))}")

        if emitted_text:
            return

        if final_text:
            for chunk in self._split_chunks(final_text):
                yield chunk
            return

        if error_messages:
            raise AgentClientError(f"{self._name} cli error: {self._truncate(' | '.join(error_messages))}")

        fallback = "\n".join(fallback_parts).strip()
        if fallback:
            for chunk in self._split_chunks(fallback):
                yield chunk
            return

        raise AgentClientError(
            f"{self._name} cli produced no output: {self._truncate(stderr_text)}"
        )

    async def _run_once(
        self,
        prompt: str,
        trace_id: str,
        *,
        session_id: str | None = None,
        session_holder: dict[str, str] | None = None,
        image_paths: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        async for piece in self._run_stream_once(
            prompt=prompt,
            trace_id=trace_id,
            session_id=session_id,
            session_holder=session_holder,
            image_paths=image_paths,
        ):
            parts.append(piece)
        return "".join(parts).strip()

    def _build_command(
        self,
        session_id: str | None = None,
        image_paths: list[str] | None = None,
    ) -> list[str]:
        command = [self._bin, "--mode", "json"]
        if session_id:
            command.extend(["--session-id", session_id])
        if self._model:
            command.extend(["--model", self._model])
        if self._thinking:
            command.extend(["--thinking", self._thinking])
        if self._tools:
            command.extend(["--tools", self._tools])
        for path in self._system_prompt_files():
            command.extend(["--append-system-prompt", path])
        # The clock rides the system channel, not the prompt: pi persists user
        # text into its own transcript, so a prompt-inline timestamp left one
        # stale copy per turn (327 in the WeChat session) for the model to pick
        # from. System prompt text is per-process, never persisted, never compacted.
        command.extend(["--append-system-prompt", time_context()])
        if self._approve_project:
            command.append("--approve")
        # pi native multimodal syntax: `pi [@files...] [messages...]`.
        # Each `@path` positional argument is loaded as an attachment before
        # the prompt string, so images reach the model as real vision input
        # instead of a text hint the model may or may not act on.
        for image_path in image_paths or []:
            command.append(f"@{image_path}")
        return command

    @staticmethod
    def _system_prompt_files() -> list[str]:
        """Inject rules and memory on every turn through pi's system channel.

        `--append-system-prompt` reads a path's contents (verified against pi
        0.83.0). These files are reread each turn, not stored in the transcript.
        """
        rules_system = os.path.join(_PROJECT_ROOT, "rules", "system.md")
        if not os.path.isfile(rules_system):
            # 静默丢规则 = 模型变得"不像自己"，比启动报错更糟。
            logger.warning(
                "rules/system.md is missing; public rules won't reach the system prompt",
                extra={"event": "pi.rules_missing"},
            )
        paths = [
            path
            for path in (
                rules_system,
                os.path.join(_PROJECT_ROOT, "rules", "admin.md"),
            )
            if os.path.isfile(path)
        ]
        memory_path = PiCliClient._memory_context_path()
        if memory_path:
            paths.append(memory_path)
        return paths

    @staticmethod
    def _memory_context_path() -> str | None:
        try:
            return memory.write_context_file()
        except Exception:  # noqa: BLE001 - memory must never break a turn
            logger.warning("failed to build memory context", extra={"event": "memory.context_build"})
            return None

    async def _spawn_process(self, command: list[str]) -> asyncio.subprocess.Process:
        env = os.environ.copy()
        if self._api_key:
            env["DASHSCOPE_API_KEY"] = self._api_key
        if self._offline:
            env["PI_OFFLINE"] = "1"
        if self._agent_dir:
            env["PI_CODING_AGENT_DIR"] = self._agent_dir
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=self._work_dir,
            env=env,
            # supervisor hands the parent an stdin pipe that never closes, and
            # pi merges piped stdin into the first prompt, so it would block
            # waiting for EOF instead of running.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._settings.pi_stream_read_limit_bytes,
            start_new_session=True,
        )

    async def _readline_with_idle_timeout(self, stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        return await asyncio.wait_for(stream.readline(), timeout=self._idle_timeout_seconds)

    async def _read_stream_text(self, stream: asyncio.StreamReader | None) -> str:
        if stream is None:
            return ""
        data = await stream.read()
        if not data:
            return ""
        return data.decode("utf-8", errors="replace")

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        self._kill_process_group(process)
        await process.wait()

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def _register_process(self, trace_id: str, process: asyncio.subprocess.Process) -> None:
        with self._process_lock:
            self._active_processes[trace_id] = process

    def _unregister_process(self, trace_id: str) -> None:
        with self._process_lock:
            self._active_processes.pop(trace_id, None)

    def _raise_if_cancelled(self, trace_id: str) -> None:
        with self._process_lock:
            if trace_id not in self._cancel_requests:
                return
        raise AgentClientCancelled(f"{self._name} cli cancelled by user")

    def _clear_cancel_request(self, trace_id: str) -> None:
        with self._process_lock:
            self._cancel_requests.discard(trace_id)

    @staticmethod
    def _parse_event(line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            return None
        return None

    @staticmethod
    def _extract_text_delta(event: dict[str, Any]) -> str:
        """pi streams real deltas; thinking and tool-call deltas are dropped."""
        if event.get("type") != "message_update":
            return ""
        inner = event.get("assistantMessageEvent")
        if not isinstance(inner, dict) or inner.get("type") != "text_delta":
            return ""
        delta = inner.get("delta")
        return delta if isinstance(delta, str) else ""

    @staticmethod
    def _assistant_message(event: dict[str, Any]) -> dict[str, Any] | None:
        # `message_end` fires for the user turn too, so the role has to be checked.
        if event.get("type") != "message_end":
            return None
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        return message

    @classmethod
    def _extract_final_text(cls, event: dict[str, Any]) -> str:
        message = cls._assistant_message(event)
        if message is None:
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _stop_reason_error(message: dict[str, Any]) -> str:
        """Error detail carried by a failed assistant `message_end`, if any."""
        stop_reason = str(message.get("stopReason", ""))
        if stop_reason not in {"error", "aborted"}:
            return ""
        detail = message.get("errorMessage")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return f"stopReason={stop_reason}"

    @classmethod
    def _extract_error_message(cls, event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))

        message = cls._assistant_message(event)
        if message is not None:
            return cls._stop_reason_error(message)

        if event_type == "extension_error":
            for key in ("message", "error"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return "extension_error"

        if event_type == "auto_retry_end" and event.get("success") is False:
            for key in ("message", "error"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return "auto_retry_end: success=false"

        return ""

    @staticmethod
    def _extract_session_id(event: dict[str, Any]) -> str:
        if event.get("type") != "session":
            return ""
        value = event.get("id")
        return value.strip() if isinstance(value, str) else ""

    def _build_native_prompt(self, messages: list[dict[str, str]], *, include_preamble: bool) -> str:
        user_text = ""
        for message in reversed(messages):
            if str(message.get("role", "user")) == "user":
                user_text = str(message.get("content", "")).strip()
                break

        lines: list[str] = []
        if include_preamble:
            # Rules and memory ride `--append-system-prompt`; only the skill
            # summary still needs prompt injection.
            skill_summary = build_skill_summary()
            if skill_summary:
                lines.extend(
                    [
                        "本机可用 skills 如下。若用户询问 skills，必须基于此列表回答，不要说当前环境没有加载 skill。",
                        skill_summary,
                        "",
                    ]
                )
        lines.append(f"用户: {user_text}")
        return "\n".join(lines)

    def _get_or_create_session_id(self, session_key: str | None) -> tuple[str | None, bool]:
        """pi accepts an unknown `--session-id` and creates it, so Ferry owns
        the ID instead of parsing one out of the stream."""
        if not session_key:
            return None, True
        with self._session_lock:
            existing = self._session_ids.get(session_key)
            if existing:
                return existing, False
            session_id = uuid.uuid4().hex
            self._session_ids[session_key] = session_id
            self._save_sessions()
            return session_id, True

    def _persist_session(
        self, session_key: str | None, session_holder: dict[str, str], *, expected_id: str | None
    ) -> None:
        if not session_key:
            return
        new_id = session_holder.get("id")
        if not new_id:
            return
        with self._session_lock:
            current = self._session_ids.get(session_key)
            # /new or /reset can run while this process is still finishing.
            # Never restore the old ID over the reset (or a newer conversation).
            if current != expected_id or current == new_id:
                return
            logger.warning(
                "pi returned a different session id",
                extra={
                    "event": "pi.session_mismatch",
                    "backend": self._name,
                    "expected": current,
                    "actual": new_id,
                },
            )
            self._session_ids[session_key] = new_id
            self._save_sessions()

    def _load_sessions(self) -> dict[str, str]:
        try:
            with open(self._session_store_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}
        return {}

    def _save_sessions(self) -> None:
        # Callers hold self._session_lock.
        try:
            os.makedirs(os.path.dirname(self._session_store_path), exist_ok=True)
            tmp_path = f"{self._session_store_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._session_ids, fh)
            os.replace(tmp_path, self._session_store_path)
        except OSError:
            logger.warning(
                "failed to persist pi session state",
                extra={"event": "pi.session_persist", "backend": self._name},
            )

    def _assert_circuit_closed(self) -> None:
        with self._lock:
            if time.time() < self._circuit_open_until:
                raise AgentClientError("circuit breaker is open")

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._settings.pi_circuit_breaker_threshold:
                self._circuit_open_until = time.time() + self._settings.pi_circuit_breaker_cooldown_seconds

    def _split_chunks(self, text: str) -> list[str]:
        content = text
        if not content:
            return []
        if len(content) <= self._stream_piece_chars:
            return [content]
        chunks: list[str] = []
        start = 0
        while start < len(content):
            chunks.append(content[start : start + self._stream_piece_chars])
            start += self._stream_piece_chars
        return chunks

    @staticmethod
    def _truncate(text: str, limit: int = 240) -> str:
        compact = " ".join(text.strip().split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: limit - 3]}..."

    @staticmethod
    def _resolve_image_paths(image_paths: list[str] | None) -> list[str]:
        """Normalize image paths for pi's `@file` multimodal syntax.

        Deduplicates while preserving order, expands `~`, resolves to absolute
        paths and drops entries that don't exist as regular files. pi is
        spawned with `cwd=pi_work_dir`, so a relative path would resolve
        against the wrong directory.
        """
        if not image_paths:
            return []
        seen: set[str] = set()
        resolved: list[str] = []
        for raw in image_paths:
            if not raw:
                continue
            candidate = os.path.abspath(os.path.expanduser(str(raw)))
            if candidate in seen:
                continue
            if not os.path.isfile(candidate):
                logger.warning(
                    "image path missing, dropped from pi @file args",
                    extra={"event": "pi.image_missing", "path": candidate},
                )
                continue
            seen.add(candidate)
            resolved.append(candidate)
        return resolved

    def _summarize_messages(self, messages: list[dict[str, str]]) -> str:
        segments: list[str] = []
        for message in messages[-6:]:
            role = str(message.get("role", "?"))
            content = str(message.get("content", ""))
            segments.append(f"{role}: {self._truncate(content, limit=80)}")
        return self._truncate(" | ".join(segments))

    @staticmethod
    def _should_retry(exc: Exception) -> bool:
        msg = str(exc).lower()
        if "unauthorized" in msg or "authentication" in msg or "invalid_api_key" in msg:
            return False
        if "circuit breaker" in msg:
            return False
        return True
