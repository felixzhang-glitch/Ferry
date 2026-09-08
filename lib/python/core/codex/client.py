from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from app.clock import time_context
from app.config import Settings

logger = logging.getLogger(__name__)


class CodexClientError(RuntimeError):
    pass


class CodexClientCancelled(CodexClientError):
    pass


class CodexClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._stream_piece_chars = 80
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requests: set[str] = set()

        self._codex_bin = settings.codex_cli_bin
        self._work_dir = os.path.abspath(settings.codex_work_dir)
        os.makedirs(self._work_dir, exist_ok=True)

    async def close(self) -> None:
        # CLI mode does not keep a persistent network client.
        return None

    def reset_backend_session(self, session_key: str) -> None:
        # Codex CLI is invoked statelessly; no native session to reset.
        return None

    async def chat(
        self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None
    ) -> str:
        self._assert_circuit_closed()

        prompt = self._build_prompt(messages)
        request_summary = self._summarize_messages(messages)
        attempt = 0
        start = time.monotonic()

        try:
            while True:
                try:
                    text = await self._run_chat_once(prompt=prompt, trace_id=trace_id)
                    self._record_success()
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "codex cli chat completed",
                        extra={
                            "trace_id": trace_id,
                            "event": "codex.chat",
                            "duration_ms": duration_ms,
                            "status_code": 0,
                            "request_summary": request_summary,
                            "response_summary": self._truncate(text),
                        },
                    )
                    return text
                except CodexClientCancelled:
                    logger.info(
                        "codex cli chat cancelled",
                        extra={"trace_id": trace_id, "event": "codex.chat", "status_code": 499},
                    )
                    raise
                except CodexClientError as exc:
                    if not self._should_retry(exc) or attempt >= self._settings.codex_max_retries:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "codex cli chat failed",
                            extra={
                                "trace_id": trace_id,
                                "event": "codex.chat",
                                "duration_ms": duration_ms,
                                "status_code": 1,
                                "error_code": type(exc).__name__,
                                "request_summary": request_summary,
                            },
                        )
                        raise
                    self._raise_if_cancelled(trace_id)
                    await asyncio.sleep(self._settings.codex_retry_backoff_seconds * (2**attempt))
                    self._raise_if_cancelled(trace_id)
                    attempt += 1
        finally:
            self._clear_cancel_request(trace_id)

    async def chat_stream(
        self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None
    ) -> AsyncIterator[str]:
        self._assert_circuit_closed()

        prompt = self._build_prompt(messages)
        request_summary = self._summarize_messages(messages)
        attempt = 0
        start = time.monotonic()

        try:
            while True:
                emitted = False
                preview_parts: list[str] = []
                try:
                    async for piece in self._run_stream_once(prompt=prompt, trace_id=trace_id):
                        emitted = True
                        if len("".join(preview_parts)) < 240:
                            preview_parts.append(piece)
                        yield piece

                    self._record_success()
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "codex cli stream completed",
                        extra={
                            "trace_id": trace_id,
                            "event": "codex.stream",
                            "duration_ms": duration_ms,
                            "status_code": 0,
                            "request_summary": request_summary,
                            "response_summary": self._truncate("".join(preview_parts)),
                        },
                    )
                    return
                except CodexClientCancelled:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "codex cli stream cancelled",
                        extra={
                            "trace_id": trace_id,
                            "event": "codex.stream",
                            "duration_ms": duration_ms,
                            "status_code": 499,
                            "request_summary": request_summary,
                            "response_summary": self._truncate("".join(preview_parts)),
                        },
                    )
                    raise
                except CodexClientError as exc:
                    if emitted:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "codex cli stream interrupted after partial output",
                            extra={
                                "trace_id": trace_id,
                                "event": "codex.stream",
                                "duration_ms": duration_ms,
                                "status_code": 1,
                                "error_code": type(exc).__name__,
                                "request_summary": request_summary,
                                "response_summary": self._truncate("".join(preview_parts)),
                            },
                        )
                        raise

                    if not self._should_retry(exc) or attempt >= self._settings.codex_max_retries:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "codex cli stream failed",
                            extra={
                                "trace_id": trace_id,
                                "event": "codex.stream",
                                "duration_ms": duration_ms,
                                "status_code": 1,
                                "error_code": type(exc).__name__,
                                "request_summary": request_summary,
                            },
                        )
                        raise

                    self._raise_if_cancelled(trace_id)
                    await asyncio.sleep(self._settings.codex_retry_backoff_seconds * (2**attempt))
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

    async def _run_chat_once(self, prompt: str, trace_id: str) -> str:
        command = self._build_command(prompt)
        process = await self._spawn_process(command)
        self._register_process(trace_id, process)
        stderr_task = asyncio.create_task(self._read_stream_text(process.stderr))

        completed_message = ""
        fallback_parts: list[str] = []
        media_refs: list[str] = []
        error_messages: list[str] = []

        try:
            while True:
                self._raise_if_cancelled(trace_id)
                line = await self._readline_with_idle_timeout(process.stdout)
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                event = self._parse_event(text)
                if event is None:
                    fallback_parts.append(text)
                    continue

                media_refs.extend(self._extract_file_uri_refs(event, known=media_refs))

                error_message = self._extract_error_message(event)
                if error_message:
                    error_messages.append(error_message)

                completed = self._extract_completed_message(event)
                if completed:
                    completed_message = completed
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            stderr_text = await stderr_task
            self._raise_if_cancelled(trace_id)
            raise CodexClientError(f"codex cli timeout: {self._truncate(stderr_text)}") from exc
        finally:
            self._unregister_process(trace_id)

        return_code = await process.wait()
        stderr_text = await stderr_task

        self._raise_if_cancelled(trace_id)
        if return_code != 0:
            error_hint = " | ".join(error_messages).strip() or "\n".join(fallback_parts).strip() or stderr_text.strip()
            raise CodexClientError(
                f"codex cli failed: return_code={return_code}, error={self._truncate(error_hint)}"
            )

        if completed_message:
            return self._join_answer_parts(completed_message, media_refs)

        fallback = "\n".join(fallback_parts).strip()
        return self._join_answer_parts(fallback, media_refs)

    async def _run_stream_once(self, prompt: str, trace_id: str) -> AsyncIterator[str]:
        command = self._build_command(prompt)
        process = await self._spawn_process(command)
        self._register_process(trace_id, process)
        stderr_task = asyncio.create_task(self._read_stream_text(process.stderr))

        saw_incremental = False
        completed_message = ""
        fallback_parts: list[str] = []
        media_refs: list[str] = []
        error_messages: list[str] = []

        try:
            while True:
                self._raise_if_cancelled(trace_id)
                line = await self._readline_with_idle_timeout(process.stdout)
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                event = self._parse_event(text)
                if event is None:
                    fallback_parts.append(text)
                    continue

                media_refs.extend(self._extract_file_uri_refs(event, known=media_refs))

                error_message = self._extract_error_message(event)
                if error_message:
                    error_messages.append(error_message)

                delta_text = self._extract_incremental_text(event)
                if delta_text:
                    saw_incremental = True
                    for chunk in self._split_chunks(delta_text):
                        yield chunk
                    continue

                completed = self._extract_completed_message(event)
                if completed:
                    completed_message = completed
                    if not saw_incremental:
                        for chunk in self._split_chunks(completed):
                            yield chunk
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            stderr_text = await stderr_task
            self._raise_if_cancelled(trace_id)
            raise CodexClientError(f"codex cli timeout: {self._truncate(stderr_text)}") from exc
        finally:
            self._unregister_process(trace_id)

        return_code = await process.wait()
        stderr_text = await stderr_task

        self._raise_if_cancelled(trace_id)
        if return_code != 0:
            error_hint = " | ".join(error_messages).strip() or "\n".join(fallback_parts).strip() or stderr_text.strip()
            raise CodexClientError(
                f"codex cli failed: return_code={return_code}, error={self._truncate(error_hint)}"
            )

        if not saw_incremental and not completed_message:
            fallback = "\n".join(fallback_parts).strip()
            if fallback:
                for chunk in self._split_chunks(fallback):
                    yield chunk

        if media_refs:
            for chunk in self._split_chunks("\n".join(media_refs)):
                yield chunk

    def _build_command(self, prompt: str) -> list[str]:
        command = [
            self._codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "-C",
            self._work_dir,
        ]

        model = self._settings.codex_model.strip()
        # Compatibility: earlier versions defaulted to codex-mini-latest, which
        # may be unavailable on ChatGPT-account logins in codex CLI.
        if model and model != "codex-mini-latest":
            command.extend(["-m", model])

        command.extend(self._permission_args())
        command.append(prompt)
        return command

    def _permission_args(self) -> list[str]:
        mode = self._settings.codex_permission_mode.strip().lower()
        if mode == "full":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if mode in {"workspace-write", "workspace_write"}:
            return ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
        if mode in {"read-only", "read_only", "readonly"}:
            return ["--sandbox", "read-only", "--ask-for-approval", "never"]
        return []

    async def _spawn_process(self, command: list[str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._settings.codex_stream_read_limit_bytes,
            start_new_session=True,
        )

    async def _readline_before_deadline(self, stream: asyncio.StreamReader | None, *, deadline: float) -> bytes:
        if stream is None:
            return b""
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(stream.readline(), timeout=remaining_seconds)

    async def _readline_with_idle_timeout(self, stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        return await asyncio.wait_for(
            stream.readline(), timeout=self._settings.codex_timeout_seconds
        )

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
        raise CodexClientCancelled("codex cli cancelled by user")

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
    def _extract_completed_message(event: dict[str, Any]) -> str:
        if event.get("type") != "item.completed":
            return ""

        item = event.get("item")
        if not isinstance(item, dict):
            return ""

        if item.get("type") != "agent_message":
            return ""

        text = item.get("text")
        if isinstance(text, str):
            return text.strip()
        return ""

    @staticmethod
    def _extract_incremental_text(event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))
        if "delta" not in event_type:
            return ""

        candidates: list[Any] = [
            event.get("delta"),
            event.get("text"),
            (event.get("item") or {}).get("delta") if isinstance(event.get("item"), dict) else None,
            (event.get("item") or {}).get("text") if isinstance(event.get("item"), dict) else None,
            (event.get("data") or {}).get("delta") if isinstance(event.get("data"), dict) else None,
            (event.get("data") or {}).get("text") if isinstance(event.get("data"), dict) else None,
        ]

        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                text = value.get("text")
                if isinstance(text, str) and text.strip():
                    return text

        return ""

    @staticmethod
    def _extract_error_message(event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))
        if event_type == "error":
            message = event.get("message")
            if isinstance(message, str):
                return message.strip()

        if event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    return message.strip()

        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "error":
                message = item.get("message")
                if isinstance(message, str):
                    return message.strip()

        return ""

    @classmethod
    def _extract_file_uri_refs(cls, event: dict[str, Any], known: list[str]) -> list[str]:
        found: list[str] = []
        known_set = set(known)
        for value in cls._walk_values(event):
            if not isinstance(value, str):
                continue
            for ref in re.findall(r"file://[^\s)>\]\"']+\.(?:png|jpe?g|gif|webp|bmp)", value, flags=re.IGNORECASE):
                if ref not in known_set and ref not in found:
                    found.append(ref)
        return found

    @classmethod
    def _walk_values(cls, value: Any) -> list[Any]:
        if isinstance(value, dict):
            values: list[Any] = []
            for child in value.values():
                values.extend(cls._walk_values(child))
            return values
        if isinstance(value, list):
            values = []
            for child in value:
                values.extend(cls._walk_values(child))
            return values
        return [value]

    @staticmethod
    def _join_answer_parts(text: str, media_refs: list[str]) -> str:
        parts = [part for part in [text.strip(), "\n".join(media_refs).strip()] if part]
        return "\n".join(parts)

    def _build_prompt(self, messages: list[dict[str, str]]) -> str:
        prompt_lines = [
            time_context(),
            "",
            "你是 CodexClaw 的后端助手。",
            "请基于以下多轮对话，直接回复最后一条用户消息。",
            "仅输出回复正文，不要加额外前缀。",
            "",
            "对话历史:",
        ]

        for message in messages:
            role = str(message.get("role", "user"))
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if role == "assistant":
                label = "助手"
            elif role == "system":
                label = "系统"
            else:
                label = "用户"
            prompt_lines.append(f"{label}: {content}")

        return "\n".join(prompt_lines)

    def _assert_circuit_closed(self) -> None:
        with self._lock:
            if time.time() < self._circuit_open_until:
                raise CodexClientError("circuit breaker is open")

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._settings.codex_circuit_breaker_threshold:
                self._circuit_open_until = time.time() + self._settings.codex_circuit_breaker_cooldown_seconds

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
        if "unauthorized" in msg or "authentication" in msg:
            return False
        if "return_code=2" in msg:
            return False
        return True
