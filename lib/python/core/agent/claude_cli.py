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
from app.rules import load_system_rules
from core.codex.client import CodexClientCancelled, CodexClientError

logger = logging.getLogger(__name__)


class ClaudeCliClient:
    """Backend client for Claude Code-family CLIs (claude / qodercli).

    Exposes the same public surface as CodexClient (chat / chat_stream / cancel /
    close) so it is interchangeable behind AgentRouter. The invocation and the
    JSON output schema differ from codex, so the command builder and stream parser
    are specific to the Claude Code family.
    """

    SKILL_ROOTS = (
        "~/.claude/skills",
        "~/.codex/skills",
        "~/.agents/skills",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "skills"),  # 项目级
    )

    def __init__(
        self,
        settings: Settings,
        *,
        name: str,
        bin_path: str,
        model: str,
        permission_mode: str,
        timeout_seconds: float | None = None,
        use_verbose: bool = True,
        use_partial_messages: bool = True,
    ) -> None:
        self._settings = settings
        self._name = name
        self._bin = bin_path
        self._model = (model or "").strip()
        self._permission_mode = (permission_mode or "").strip()
        self._timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.codex_timeout_seconds
        self._use_verbose = use_verbose
        self._use_partial_messages = use_partial_messages

        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._stream_piece_chars = 80
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requests: set[str] = set()

        self._work_dir = os.path.abspath(os.path.join(settings.codex_work_dir, self._name))
        os.makedirs(self._work_dir, exist_ok=True)

    @property
    def name(self) -> str:
        return self._name

    async def close(self) -> None:
        return None

    def reset_backend_session(self, session_key: str) -> None:
        # Claude/Qoder CLI are invoked statelessly; no native session to reset.
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
                        "claude cli chat completed",
                        extra={
                            "trace_id": trace_id,
                            "event": "claude.chat",
                            "backend": self._name,
                            "duration_ms": duration_ms,
                            "status_code": 0,
                            "request_summary": request_summary,
                            "response_summary": self._truncate(text),
                        },
                    )
                    return text
                except CodexClientCancelled:
                    logger.info(
                        "claude cli chat cancelled",
                        extra={"trace_id": trace_id, "event": "claude.chat", "backend": self._name, "status_code": 499},
                    )
                    raise
                except CodexClientError as exc:
                    if not self._should_retry(exc) or attempt >= self._settings.codex_max_retries:
                        self._record_failure()
                        duration_ms = int((time.monotonic() - start) * 1000)
                        logger.error(
                            "claude cli chat failed",
                            extra={
                                "trace_id": trace_id,
                                "event": "claude.chat",
                                "backend": self._name,
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
                        "claude cli stream completed",
                        extra={
                            "trace_id": trace_id,
                            "event": "claude.stream",
                            "backend": self._name,
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
                        "claude cli stream cancelled",
                        extra={
                            "trace_id": trace_id,
                            "event": "claude.stream",
                            "backend": self._name,
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
                            "claude cli stream interrupted after partial output",
                            extra={
                                "trace_id": trace_id,
                                "event": "claude.stream",
                                "backend": self._name,
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
                            "claude cli stream failed",
                            extra={
                                "trace_id": trace_id,
                                "event": "claude.stream",
                                "backend": self._name,
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
        command = self._build_command(prompt, streaming=False)
        process = await self._spawn_process(command)
        self._register_process(trace_id, process)
        stderr_task = asyncio.create_task(self._read_stream_text(process.stderr))

        completed_message = ""
        fallback_parts: list[str] = []
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

                error_message = self._extract_error_message(event)
                if error_message:
                    error_messages.append(error_message)

                completed = self._extract_result_message(event)
                if completed:
                    completed_message = completed
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            stderr_text = await stderr_task
            self._raise_if_cancelled(trace_id)
            raise CodexClientError(f"{self._name} cli timeout: {self._truncate(stderr_text)}") from exc
        finally:
            self._unregister_process(trace_id)

        return_code = await process.wait()
        stderr_text = await stderr_task

        self._raise_if_cancelled(trace_id)
        if return_code != 0:
            error_hint = " | ".join(error_messages).strip() or "\n".join(fallback_parts).strip() or stderr_text.strip()
            raise CodexClientError(
                f"{self._name} cli failed: return_code={return_code}, error={self._truncate(error_hint)}"
            )
        if error_messages and not completed_message:
            raise CodexClientError(f"{self._name} cli error: {self._truncate(' | '.join(error_messages))}")

        if completed_message:
            return completed_message
        return "\n".join(fallback_parts).strip()

    async def _run_stream_once(self, prompt: str, trace_id: str) -> AsyncIterator[str]:
        command = self._build_command(prompt, streaming=True)
        process = await self._spawn_process(command)
        self._register_process(trace_id, process)
        stderr_task = asyncio.create_task(self._read_stream_text(process.stderr))

        saw_incremental = False
        completed_message = ""
        fallback_parts: list[str] = []
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

                error_message = self._extract_error_message(event)
                if error_message:
                    error_messages.append(error_message)

                delta_text = self._extract_text_delta(event)
                if delta_text:
                    saw_incremental = True
                    for chunk in self._split_chunks(delta_text):
                        yield chunk
                    continue

                completed = self._extract_result_message(event)
                if completed:
                    completed_message = completed
                    if not saw_incremental:
                        for chunk in self._split_chunks(completed):
                            yield chunk
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            stderr_text = await stderr_task
            self._raise_if_cancelled(trace_id)
            raise CodexClientError(f"{self._name} cli timeout: {self._truncate(stderr_text)}") from exc
        finally:
            self._unregister_process(trace_id)

        return_code = await process.wait()
        stderr_text = await stderr_task

        self._raise_if_cancelled(trace_id)
        if return_code != 0:
            error_hint = " | ".join(error_messages).strip() or "\n".join(fallback_parts).strip() or stderr_text.strip()
            raise CodexClientError(
                f"{self._name} cli failed: return_code={return_code}, error={self._truncate(error_hint)}"
            )
        if error_messages and not saw_incremental and not completed_message:
            raise CodexClientError(f"{self._name} cli error: {self._truncate(' | '.join(error_messages))}")

        if not saw_incremental and not completed_message:
            fallback = "\n".join(fallback_parts).strip()
            if fallback:
                for chunk in self._split_chunks(fallback):
                    yield chunk

    def _build_command(self, prompt: str, *, streaming: bool) -> list[str]:
        command = [
            self._bin,
            "-p",
            "--add-dir",
            self._work_dir,
        ]
        if streaming:
            command.extend(["--output-format", "stream-json"])
            if self._use_partial_messages:
                command.append("--include-partial-messages")
            if self._use_verbose:
                command.append("--verbose")
        else:
            command.extend(["--output-format", "json"])

        if self._model:
            command.extend(["--model", self._model])
        if self._permission_mode:
            skip_aliases = {"skip", "dangerously_skip", "bypass_permissions", "dangerously-skip-permissions"}
            if self._permission_mode.lower().replace(" ", "_") in skip_aliases:
                command.append("--dangerously-skip-permissions")
            else:
                command.extend(["--permission-mode", self._permission_mode])

        command.append(prompt)
        return command

    async def _spawn_process(self, command: list[str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=self._work_dir,
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
        return await asyncio.wait_for(stream.readline(), timeout=self._timeout_seconds)

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
        raise CodexClientCancelled(f"{self._name} cli cancelled by user")

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
    def _extract_result_message(event: dict[str, Any]) -> str:
        if event.get("type") != "result":
            return ""
        if event.get("is_error") is True:
            return ""
        result = event.get("result")
        if isinstance(result, str):
            return result.strip()
        return ""

    @staticmethod
    def _extract_text_delta(event: dict[str, Any]) -> str:
        if event.get("type") != "stream_event":
            return ""
        inner = event.get("event")
        if not isinstance(inner, dict):
            return ""
        if inner.get("type") != "content_block_delta":
            return ""
        delta = inner.get("delta")
        if not isinstance(delta, dict):
            return ""
        if delta.get("type") != "text_delta":
            return ""
        text = delta.get("text")
        if isinstance(text, str):
            return text
        return ""

    @staticmethod
    def _extract_error_message(event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))
        if event_type == "result" and event.get("is_error") is True:
            for key in ("result", "error", "api_error_status"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            subtype = event.get("subtype")
            if isinstance(subtype, str) and subtype.strip():
                return subtype.strip()
            return "unknown error"
        if event_type == "error":
            message = event.get("message")
            if isinstance(message, str):
                return message.strip()
        return ""

    def _build_prompt(self, messages: list[dict[str, str]]) -> str:
        rules = load_system_rules()
        # This family has no session transcript of its own (codeClaw replays raw
        # messages each turn), so the clock can ride the prompt without piling up.
        prompt_lines = [time_context(), ""]
        if rules:
            prompt_lines.extend([rules, "", "请基于以下多轮对话，直接回复最后一条用户消息。"])
        else:
            prompt_lines.extend(
                [
                    "你是 codeClaw 的后端助手。",
                    "请基于以下多轮对话，直接回复最后一条用户消息。",
                    "仅输出回复正文，不要加额外前缀。",
                ]
            )
        skill_summary = self._build_skill_summary()
        if skill_summary:
            prompt_lines.extend(
                [
                    "",
                    "本机可用 skills 如下。若用户询问 skills，必须基于此列表回答，不要说当前环境没有加载 skill。",
                    skill_summary,
                ]
            )
        prompt_lines.extend(["", "对话历史:"])

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

    @classmethod
    def _build_skill_summary(cls, *, limit: int = 80) -> str:
        skills: dict[str, str] = {}
        for root in cls.SKILL_ROOTS:
            root_path = os.path.expanduser(root)
            if not os.path.isdir(root_path):
                continue
            for dirpath, _, filenames in os.walk(root_path):
                if "SKILL.md" not in filenames:
                    continue
                skill_path = os.path.join(dirpath, "SKILL.md")
                name, description = cls._read_skill_metadata(skill_path)
                if not name:
                    name = os.path.basename(dirpath)
                skills.setdefault(name, description)
                if len(skills) >= limit:
                    break
            if len(skills) >= limit:
                break

        if not skills:
            return ""
        lines: list[str] = []
        for name, description in sorted(skills.items(), key=lambda item: item[0].lower()):
            if description:
                lines.append(f"- `{name}`: {cls._truncate(description, limit=180)}")
            else:
                lines.append(f"- `{name}`")
        return "\n".join(lines)

    @classmethod
    def _read_skill_metadata(cls, skill_path: str) -> tuple[str, str]:
        try:
            with open(skill_path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return "", ""

        if not lines or lines[0].strip() != "---":
            return "", ""

        name = ""
        description = ""
        idx = 1
        while idx < len(lines):
            line = lines[idx].rstrip("\n")
            stripped = line.strip()
            if stripped == "---":
                break
            if stripped.startswith("name:"):
                name = cls._clean_yaml_value(stripped.split(":", 1)[1])
            elif stripped.startswith("description:"):
                raw_value = stripped.split(":", 1)[1].strip()
                if raw_value in {">", ">-", "|", "|-"}:
                    idx += 1
                    parts: list[str] = []
                    while idx < len(lines):
                        next_line = lines[idx].rstrip("\n")
                        next_stripped = next_line.strip()
                        if next_stripped == "---":
                            idx -= 1
                            break
                        if next_line and not next_line.startswith((" ", "\t")):
                            idx -= 1
                            break
                        if next_stripped:
                            parts.append(next_stripped)
                        idx += 1
                    description = " ".join(parts)
                else:
                    description = cls._clean_yaml_value(raw_value)
            idx += 1

        return name, description

    @staticmethod
    def _clean_yaml_value(value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            return cleaned[1:-1].strip()
        return cleaned

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
        if "circuit breaker" in msg:
            return False
        return True
