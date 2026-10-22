from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import shlex
import threading
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from app import memory
from app.clock import time_context
from app.config import Settings
from app.skills import SKILL_ROOTS, build_skill_summary
from core.agent.types import AgentClientCancelled, AgentClientError, FinalTextCallback, ProgressCallback, ToolEventCallback
from core.agent.pi_worker import PiWorkerLease, PiWorkerPool
from observability.telemetry import observe_agent, observe_attempt, record_pi_event

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
SESSION_HISTORY_FILENAME = "pi-session-history.jsonl"
_TOOL_DESCRIPTIONS = {"bash": "执行本地命令", "read": "读取文件", "write": "写入文件", "edit": "编辑文件",
                      "grep": "检索内容", "find": "查找文件", "ls": "查看目录", "web": "查询网页"}
_SENSITIVE_ARGUMENT = r"(?:password|passwd|secret|token|api[_-]?key|authorization|credential|cookie|session[_-]?id|access[_-]?key|private[_-]?key|密码|密钥)"
_ARGUMENT_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)'''
_PRIVATE_FILE = re.compile(r"(?i)(^\.env(?:[.-]|$)|^(?:admin|auth|account|credentials?)(?:[._-]|$)|^id_(?:rsa|ed25519)|\.(?:pem|key|p12|pfx)$)")


def _tool_file_name(value: str) -> str:
    name = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return "[私有文件]" if _PRIVATE_FILE.search(name) else name or "目录"


def _tool_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        name = _tool_file_name(parsed.path) if parsed.path else ""
        return (parsed.hostname or "网页") + (f"/{name}" if name else "")
    except ValueError:
        return "[网页地址]"


def sanitize_tool_summary(value: str) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]", " ", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9_+/=.:-]+", "[认证信息已隐藏]", text)
    text = re.sub(r"(?i)eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|\bsk-[A-Za-z0-9_-]{12,}|\bAKIA[0-9A-Z]{16}", "[密钥已隐藏]", text)
    assignment = rf'''(?i)(["']?[\w.-]*{_SENSITIVE_ARGUMENT}[\w.-]*["']?\s*[:=]\s*){_ARGUMENT_VALUE}'''
    flags = rf"(?i)((?:--?[\w.-]*{_SENSITIVE_ARGUMENT}[\w.-]*|--user|--proxy-user|-u|-p|-b)\s+){_ARGUMENT_VALUE}"
    text = re.sub(assignment, r"\1[已隐藏]", text)
    text = re.sub(flags, r"\1[已隐藏]", text)
    text = re.sub(rf"\b([A-Z][A-Z0-9_]{{1,}}\s*=\s*){_ARGUMENT_VALUE}", r"\1[已隐藏]", text)
    text = re.sub(r'''https?://[^\s<>"']+''', lambda match: _tool_url(match.group()), text)
    text = re.sub(r'''(?<![\w:])(?:~?/|[A-Za-z]:\\)(?:[^\s<>"'()\[\],;]+)''', "[本地路径]", text)
    text = re.sub(r"(?i)(?<![\w])\.env(?:[\w.-]*)|\b(?:admin|auth|account|credentials?)\.(?:md|json|env|txt)|\bid_(?:rsa|ed25519)\b", "[私有文件]", text)
    return " ".join(text.split())[:160]


def _tool_argument_preview(tokens: list[str]) -> str:
    preview = []
    hide_next = False
    for token in tokens[:8]:
        if hide_next:
            preview.append("[已隐藏]"); hide_next = False
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            preview.append(token.split("=", 1)[0] + "=[已隐藏]")
            continue
        if token.startswith("-") and (re.search(_SENSITIVE_ARGUMENT, token, re.I) or token in {"-u", "-p", "-b", "--user", "--proxy-user"}):
            preview.append(token.split("=", 1)[0] + ("=[已隐藏]" if "=" in token else ""))
            hide_next = "=" not in token
            continue
        if re.match(r"^-p[^-].+", token):
            preview.append("-p[已隐藏]")
            continue
        if any(char in token for char in ("{", "}", "[", "]", "\n")):
            preview.append("[参数内容已省略]")
            continue
        if token.startswith(("http://", "https://")):
            token = _tool_url(token)
        elif "/" in token or re.match(r"^[A-Za-z]:\\", token):
            token = _tool_file_name(token)
        if token:
            preview.append(token)
    return sanitize_tool_summary(" ".join(preview))


def _tool_command_summary(command: str) -> str:
    if len(command) > 4096 or any(char in command for char in ("\n", "\r", "`", "$", "<<")):
        return "执行本地命令"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True; lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return "执行本地命令"
    if len(tokens) > 96:
        return "执行本地命令"
    while tokens and (tokens[0] == "env" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])):
        tokens.pop(0)
    if tokens and _tool_file_name(tokens[0]) == "cd":
        if "&&" not in tokens:
            return "切换工作目录"
        tokens = tokens[tokens.index("&&") + 1:]
    if not tokens:
        return "执行本地命令"
    stop = next((i for i, token in enumerate(tokens) if token and all(c in ";&|()<>" for c in token)), len(tokens))
    tokens = tokens[:stop]
    if not tokens:
        return "执行本地命令"
    executable = _tool_file_name(tokens[0])
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", executable):
        return "执行本地命令"
    wrappers = ("python", "node", "bash", "sh", "npx", "uv")
    if executable.startswith(wrappers):
        if any(re.fullmatch(r"-[A-Za-z]*[cep][A-Za-z]*", flag) or flag.startswith(("--eval", "--print")) for flag in tokens[1:3]):
            return f"{executable} · 执行代码"
        script = next((i for i, token in enumerate(tokens[1:], 1) if re.search(r"\.(?:py|mjs|js|sh)$", token)), None)
        if script is not None:
            file = _tool_file_name(tokens[script])
            detail = _tool_argument_preview(tokens[script + 1:])
            return sanitize_tool_summary(f"运行 {file}" + (f" · {detail}" if detail else ""))
    detail = _tool_argument_preview(tokens[1:])
    if executable == "git" and len(tokens) > 1 and tokens[1] == "diff":
        return sanitize_tool_summary("git diff · 检查代码差异" + (f" · {detail.removeprefix('diff').strip()}" if detail != "diff" else ""))
    return sanitize_tool_summary(executable + (f" · {detail}" if detail else ""))


def _tool_summary(name: str, args: Any) -> str:
    fallback = _TOOL_DESCRIPTIONS.get(name, "搜索资料" if "search" in name.lower() else "调用工具")
    if not isinstance(args, dict):
        return fallback
    if name == "bash" and isinstance(args.get("command"), str):
        return _tool_command_summary(args["command"])
    path = next((args.get(key) for key in ("path", "file_path", "filePath") if isinstance(args.get(key), str)), None)
    query = next((args.get(key) for key in ("query", "search_query", "keyword", "pattern") if isinstance(args.get(key), str)), None)
    if query and ("search" in name.lower() or name in {"grep", "find", "web"}):
        return sanitize_tool_summary(f"{'搜索' if 'search' in name.lower() else fallback}：{query}") or fallback
    if path and name in {"read", "write", "edit", "ls", "find", "grep"}:
        label = _tool_file_name(path)
        if label == "SKILL.md":
            parent = path.replace("\\", "/").rstrip("/").rsplit("/", 2)
            if len(parent) >= 2:
                label += f"（{_tool_file_name(parent[-2])}）"
        return sanitize_tool_summary(f"{fallback}：{label}") or fallback
    if isinstance(args.get("url"), str):
        return sanitize_tool_summary(f"{fallback}：{_tool_url(args['url'])}") or fallback
    return fallback


class PiCliClient:
    """Backend client for pi's native JSON events, via SDK workers or CLI.

    Owns native session IDs, worker/CLI lifecycle, retries and event parsing.
    Persistent mode leases a preloaded Node worker; CLI mode remains a rollback.
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
        self._deepseek_api_key = settings.pi_deepseek_api_key.strip()
        self._bailian_plan_api_key = settings.pi_bailian_plan_api_key.strip()
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
        self._active_processes: dict[str, asyncio.subprocess.Process | PiWorkerLease] = {}
        self._pending_spawns: dict[str, asyncio.Task] = {}
        self._cancel_requests: set[str] = set()
        self._worker_pool: PiWorkerPool | None = None
        self._closed = False

        self._work_dir = os.path.abspath(os.path.expanduser(settings.pi_work_dir))
        os.makedirs(self._work_dir, exist_ok=True)

        self._session_store_path = os.path.abspath(settings.pi_session_store_path)
        self._session_lock = threading.Lock()
        self._session_ids: dict[str, str] = self._load_sessions()

    @property
    def name(self) -> str:
        return self._name

    def set_runtime_options(self, *, model: str, thinking: str) -> None:
        """Console changes apply from the next turn; the command line is built per turn."""
        self._model = (model or "").strip()
        self._thinking = (thinking or "").strip()

    async def start(self) -> None:
        """Preload pi before channels and schedulers start accepting messages."""
        pool = self._get_worker_pool()
        if pool is not None:
            await pool.warmup()

    async def close(self) -> None:
        self._closed = True
        with self._process_lock:
            processes = list(self._active_processes.items())
            pending = list(self._pending_spawns.items())
            self._cancel_requests.update(trace_id for trace_id, _ in processes + pending)
        for _, task in pending:
            task.cancel()
        await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        await asyncio.gather(*(self._terminate_process(process) for _, process in processes))
        if self._worker_pool is not None:
            await self._worker_pool.close()

    def reset_session(self, session_key: str) -> None:
        with self._session_lock:
            existing = self._session_ids.get(session_key)
            if existing:
                # Sessions created before the ledger existed are only known to
                # the live mapping; keep them attributable once /new drops them.
                self._record_session_history(session_key, existing)
                del self._session_ids[session_key]
                self._save_sessions()

    def retire_session(self, session_key: str) -> None:
        with self._session_lock:
            if not self._record_session_history(session_key, "", event="retired"):
                raise AgentClientError("session retirement unavailable")
            self._session_ids.pop(session_key, None)
            self._save_sessions()

    @observe_agent()
    async def chat(
        self,
        messages: list[dict[str, str]],
        trace_id: str,
        *,
        session_key: str | None = None,
        image_paths: list[str] | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> str:
        self._assert_circuit_closed()
        # A runtime change must not switch models partway through a retried turn.
        model = self._model if model is None else model
        thinking = self._thinking if thinking is None else thinking

        session_id, is_new = self._get_or_create_session_id(session_key)
        session_holder: dict[str, str] = {}
        prompt = await self._build_native_prompt_async(messages, include_preamble=is_new)
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
                        model=model,
                        thinking=thinking,
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

    @observe_agent(stream=True)
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        trace_id: str,
        *,
        session_key: str | None = None,
        image_paths: list[str] | None = None,
        on_progress: ProgressCallback | None = None,
        on_tool_event: ToolEventCallback | None = None,
        model: str | None = None,
        thinking: str | None = None,
        on_final_text: FinalTextCallback | None = None,
    ) -> AsyncIterator[str]:
        self._assert_circuit_closed()
        # A runtime change must not switch models partway through a retried turn.
        model = self._model if model is None else model
        thinking = self._thinking if thinking is None else thinking

        session_id, is_new = self._get_or_create_session_id(session_key)
        session_holder: dict[str, str] = {}
        prompt = await self._build_native_prompt_async(messages, include_preamble=is_new)
        request_summary = self._summarize_messages(messages)
        resolved_images = self._resolve_image_paths(image_paths)
        attempt = 0
        start = time.monotonic()

        try:
            while True:
                emitted = False
                preview_parts: list[str] = []
                final_text: str | None = None

                async def capture_final_text(text: str) -> None:
                    nonlocal final_text
                    final_text = text

                try:
                    stream = self._run_stream_once(
                        prompt=prompt,
                        trace_id=trace_id,
                        session_id=session_id,
                        session_holder=session_holder,
                        image_paths=resolved_images,
                        on_progress=on_progress,
                        on_tool_event=on_tool_event,
                        model=model,
                        thinking=thinking,
                        on_final_text=capture_final_text if on_final_text is not None else None,
                    )
                    async with contextlib.aclosing(stream):
                        async for piece in stream:
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
                else:
                    # Consumer callback failures must not trigger another model run.
                    if on_final_text is not None and final_text is not None:
                        await on_final_text(final_text)
                    return
        finally:
            self._clear_cancel_request(trace_id)

    async def complete(
        self,
        prompt: str,
        *,
        system_prompts: list[str] | None = None,
        attachments: list[str] | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """One-shot, tool-less pi run outside the chat pipeline.

        No session, no rules/memory injection, no worker pool and no circuit
        breaker: background jobs must neither touch chat state nor trip the
        breaker that guards live conversations.
        """
        if self._closed:
            raise AgentClientError("pi client is closed")
        command = [
            self._bin, "--mode", "json", "--no-session", "--no-skills",
            "--no-tools", "--no-mcp", "--no-context-files",
        ]
        chosen_model = (model or self._model).strip()
        if chosen_model:
            command.extend(["--model", chosen_model])
        if self._thinking:
            command.extend(["--thinking", self._thinking])
        for value in system_prompts or []:
            command.extend(["--append-system-prompt", value])
        command.extend(["--append-system-prompt", time_context()])
        if self._approve_project:
            command.append("--approve")
        command.extend(f"@{path}" for path in attachments or [])
        command.append(prompt)

        timeout = timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._work_dir,
            env=self._process_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._settings.pi_stream_read_limit_bytes,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AgentClientError(f"{self._name} one-shot timeout") from exc
        finally:
            await self._terminate_process(process)

        final_text = ""
        final_error = ""
        errors: list[str] = []
        for raw in stdout.decode("utf-8", errors="replace").splitlines():
            event = self._parse_event(raw.strip())
            if event is None:
                continue
            record_pi_event(event)
            message = self._assistant_message(event)
            if message is not None:
                # The last assistant message_end is the verdict (pi may auto-retry).
                final_error = self._stop_reason_error(message)
                text = self._extract_final_text(event)
                if text:
                    final_text = text
            else:
                error = self._extract_error_message(event)
                if error:
                    errors.append(error)

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = " | ".join(errors) or stderr_text
            raise AgentClientError(
                f"{self._name} one-shot failed: return_code={process.returncode}, error={self._truncate(detail)}"
            )
        if final_error:
            raise AgentClientError(f"{self._name} one-shot error: {self._truncate(final_error)}")
        if not final_text.strip():
            detail = " | ".join(errors) or stderr_text
            raise AgentClientError(f"{self._name} one-shot produced no output: {self._truncate(detail)}")
        return final_text.strip()

    def cancel(self, trace_id: str) -> bool:
        with self._process_lock:
            process = self._active_processes.get(trace_id)
            pending = self._pending_spawns.get(trace_id)
            if process is None and pending is None:
                return False
            self._cancel_requests.add(trace_id)

        if pending is not None:
            pending.get_loop().call_soon_threadsafe(pending.cancel)
        if process is not None and process.returncode is None:
            self._kill_process_group(process)
        return True

    @observe_attempt
    async def _run_stream_once(
        self,
        prompt: str,
        trace_id: str,
        *,
        session_id: str | None = None,
        session_holder: dict[str, str] | None = None,
        image_paths: list[str] | None = None,
        on_progress: ProgressCallback | None = None,
        on_tool_event: ToolEventCallback | None = None,
        model: str | None = None,
        thinking: str | None = None,
        on_final_text: FinalTextCallback | None = None,
    ) -> AsyncIterator[str]:
        system_paths = await asyncio.to_thread(self._system_prompt_files)
        command = self._build_command(
            session_id=session_id, image_paths=image_paths, system_prompt_paths=system_paths,
            model=model, thinking=thinking,
        )
        command.append(prompt)
        if self._closed:
            raise AgentClientError("pi client is closed")
        spawn_task = asyncio.create_task(self._spawn_process(command))
        with self._process_lock:
            self._pending_spawns[trace_id] = spawn_task
        try:
            process = await spawn_task
            self._register_process(trace_id, process)
        except asyncio.CancelledError:
            # Cancellation can arrive after spawn produced a process but before
            # this coroutine resumed to register it. Do not leak that process.
            if spawn_task.done() and not spawn_task.cancelled():
                with contextlib.suppress(Exception):
                    await self._terminate_process(spawn_task.result())
            self._raise_if_cancelled(trace_id)
            raise
        finally:
            with self._process_lock:
                self._pending_spawns.pop(trace_id, None)
        stderr_task = asyncio.create_task(self._read_stream_text(process.stderr))
        turn_start = time.monotonic()
        deadline = turn_start + self._timeout_seconds

        error_messages: list[str] = []
        fallback_parts: list[str] = []
        final_text = ""
        tool_started_at: dict[str, float] = {}
        tool_names: dict[str, str] = {}
        tool_scope = uuid.uuid4().hex
        # Decided by the *last* assistant message_end: pi can fail an attempt,
        # auto-retry and then succeed, so an intermediate error is not the
        # verdict for the turn.
        final_stop_reason = ""
        final_error = ""
        emitted_text = False

        try:
            while True:
                self._raise_if_cancelled(trace_id)
                line = await self._readline_with_idle_timeout(
                    process.stdout, remaining=max(0.0, deadline - time.monotonic()),
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
                record_pi_event(event)

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
                    # An empty final message must also replace previous attempt/tool text.
                    final_text = self._extract_final_text(event)
                else:
                    error_message = self._extract_error_message(event)
                    if error_message:
                        error_messages.append(error_message)

                tool_event = self._tool_execution(event)
                if tool_event is not None:
                    phase, call_id, tool_name, detail, is_error = tool_event
                    elapsed_ms = int((time.monotonic() - turn_start) * 1000)
                    if phase == "start":
                        tool_started_at.setdefault(call_id, time.monotonic())
                        tool_names.setdefault(call_id, tool_name)
                        logger.info(
                            "pi tool call",
                            extra={
                                "trace_id": trace_id,
                                "event": "pi.tool_call",
                                "backend": self._name,
                                "tool": tool_name,
                                "tool_detail": self._truncate(detail, 160),
                                "elapsed_ms": elapsed_ms,
                            },
                        )
                        label = f"{tool_name} · {detail}" if detail else tool_name
                        await self._emit_progress(on_progress, self._truncate(label, 140), trace_id)
                        await self._emit_tool_event(on_tool_event, tool_scope, call_id, tool_name, "start", None, False,
                                                    trace_id, summary=_tool_summary(tool_name, event.get("args")))
                    else:
                        started_at = tool_started_at.pop(call_id, None)
                        tool_names.pop(call_id, None)
                        logger.info(
                            "pi tool finished",
                            extra={
                                "trace_id": trace_id,
                                "event": "pi.tool_result",
                                "backend": self._name,
                                "tool": tool_name,
                                "is_error": is_error,
                                "duration_ms": (
                                    int((time.monotonic() - started_at) * 1000) if started_at else None
                                ),
                                "elapsed_ms": elapsed_ms,
                            },
                        )
                        await self._emit_progress(on_progress, None, trace_id)
                        duration = max(0.0, time.monotonic() - started_at) if started_at is not None else None
                        await self._emit_tool_event(on_tool_event, tool_scope, call_id, tool_name, "end", duration, is_error, trace_id)

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
        except AgentClientError:
            self._raise_if_cancelled(trace_id)
            raise
        except (OSError, ValueError) as exc:
            self._raise_if_cancelled(trace_id)
            raise AgentClientError(f"{self._name} transport read failed") from exc
        finally:
            # Also reap the process on task cancellation or a closed stream.
            try:
                await self._terminate_process(process)
            finally:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                self._unregister_process(trace_id)
            for call_id, started_at in tool_started_at.items():
                await self._emit_tool_event(on_tool_event, tool_scope, call_id, tool_names[call_id], "interrupted",
                                            max(0.0, time.monotonic() - started_at), True, trace_id)

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

        if on_final_text is not None and final_stop_reason in {"stop", "length", "toolUse"}:
            # Commit only after CLI exit / worker ferry_done, not merely message_end.
            await on_final_text(final_text)
            if not final_text and not emitted_text:
                return

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
        model: str | None = None,
        thinking: str | None = None,
    ) -> str:
        parts: list[str] = []
        stream = self._run_stream_once(
            prompt=prompt,
            trace_id=trace_id,
            session_id=session_id,
            session_holder=session_holder,
            image_paths=image_paths,
            model=model,
            thinking=thinking,
        )
        async with contextlib.aclosing(stream):
            async for piece in stream:
                parts.append(piece)
        return "".join(parts).strip()

    def _build_command(
        self,
        session_id: str | None = None,
        image_paths: list[str] | None = None,
        system_prompt_paths: list[str] | None = None,
        *,
        model: str | None = None,
        thinking: str | None = None,
    ) -> list[str]:
        command = [self._bin, "--mode", "json", "--no-skills"]
        # Repo skills ride the system-prompt summary (app.skills); pi's own
        # discovery is disabled so ~/.agents/skills and other global CLI dirs
        # never leak skills into the bridge.
        if session_id:
            command.extend(["--session-id", session_id])
        # Only None inherits the shared runtime defaults; "off" is an explicit override.
        chosen_model = self._model if model is None else model.strip()
        chosen_thinking = self._thinking if thinking is None else thinking.strip()
        if chosen_model:
            command.extend(["--model", chosen_model])
        if chosen_thinking:
            command.extend(["--thinking", chosen_thinking])
        if self._tools:
            command.extend(["--tools", self._tools])
        # Async callers pass prebuilt paths: building them runs git (memory
        # auto-commit) and must stay off the event loop. The default keeps the
        # sync path for tests and direct command inspection.
        for path in system_prompt_paths if system_prompt_paths is not None else self._system_prompt_files():
            command.extend(["--append-system-prompt", path])
        # The clock rides the system channel, not the prompt: pi persists user
        # text into its own transcript, so a prompt-inline timestamp left one
        # stale copy per turn (327 in the WeChat session) for the model to pick
        # from. System text is rebuilt per turn, never persisted or compacted.
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
                "rules/system.md is missing; local rules won't reach the system prompt",
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

    def _process_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Observability authentication never needs to reach the model worker.
        for key in list(env):
            if key.startswith("OBSERVABILITY_"):
                env.pop(key, None)
        if self._api_key:
            env["DASHSCOPE_API_KEY"] = self._api_key
        if self._deepseek_api_key:
            env["DEEPSEEK_API_KEY"] = self._deepseek_api_key
        if self._bailian_plan_api_key:
            env["BAILIAN_PLAN_API_KEY"] = self._bailian_plan_api_key
        if self._offline:
            env["PI_OFFLINE"] = "1"
        if self._agent_dir:
            env["PI_CODING_AGENT_DIR"] = self._agent_dir
        return env

    def _get_worker_pool(self) -> PiWorkerPool | None:
        if not getattr(self._settings, "pi_persistent_enabled", False):
            return None
        if self._closed:
            raise AgentClientError("pi client is closed")
        if self._worker_pool is None:
            sdk = self._settings.pi_sdk_module.strip()
            if not sdk or not os.path.isabs(sdk) or not os.path.isfile(sdk):
                raise AgentClientError("PI_SDK_MODULE must name the installed pi dist/index.js")
            self._worker_pool = PiWorkerPool(
                node_bin=self._settings.pi_node_bin,
                sdk_module=sdk,
                size=self._settings.pi_worker_pool_size,
                startup_timeout=self._settings.pi_worker_startup_timeout_seconds,
                cwd=self._work_dir, env=self._process_env(),
                read_limit=self._settings.pi_stream_read_limit_bytes,
            )
        return self._worker_pool

    async def _refresh_worker_system_args(self, command: list[str]) -> list[str]:
        # A pool lease may wait minutes behind another chat. Rebuild dynamic
        # system input after acquisition, not at the time it joined the queue.
        # Memory auto-commit runs git here, so this must stay off the loop.
        system_paths = await asyncio.to_thread(self._system_prompt_files)
        system_args = []
        for value in [*system_paths, time_context()]:
            system_args.extend(["--append-system-prompt", value])
        refreshed = []
        inserted = False
        args = iter(command[1:-1])  # Never interpret the user's final prompt.
        for arg in args:
            if arg == "--append-system-prompt":
                next(args)
                if not inserted:
                    refreshed.extend(system_args)
                    inserted = True
            else:
                refreshed.append(arg)
        if not inserted:
            refreshed.extend(system_args)
        refreshed.append(command[-1])
        return refreshed

    async def _spawn_process(self, command: list[str]) -> asyncio.subprocess.Process | PiWorkerLease:
        pool = self._get_worker_pool()
        if pool is not None:
            return await pool.run(command[1:], prepare=lambda: self._refresh_worker_system_args(command))
        env = self._process_env()
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

    async def _readline_with_idle_timeout(self, stream, *, remaining: float | None = None) -> bytes:
        if stream is None:
            return b""
        timeout = self._idle_timeout_seconds
        if remaining is not None:
            timeout = min(timeout, remaining)
        read = asyncio.create_task(stream.readline())
        try:
            # Python 3.10 wait_for can swallow an external cancellation when
            # its inner read finishes in the same loop tick. wait does not.
            done, _ = await asyncio.wait({read}, timeout=timeout)
            if not done:
                raise asyncio.TimeoutError
            return read.result()
        finally:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)

    async def _read_stream_text(self, stream: asyncio.StreamReader | None) -> str:
        if stream is None:
            return ""
        data = await stream.read()
        if not data:
            return ""
        return data.decode("utf-8", errors="replace")

    async def _terminate_process(self, process: asyncio.subprocess.Process | PiWorkerLease) -> None:
        if isinstance(process, PiWorkerLease):
            await process.close()
            return
        if process.returncode is not None:
            return
        self._kill_process_group(process)
        await process.wait()

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process | PiWorkerLease) -> None:
        if isinstance(process, PiWorkerLease):
            process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def _register_process(self, trace_id: str, process: asyncio.subprocess.Process | PiWorkerLease) -> None:
        if isinstance(process, PiWorkerLease):
            process.trace_id = trace_id
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
    def _tool_execution(event: dict[str, Any]) -> tuple[str, str, str, str, bool] | None:
        """Decode pi's `tool_execution_start` / `tool_execution_end` events.

        Returns (phase, call_id, tool_name, detail, is_error). `detail` is the
        single argument worth showing a user: a bash command, a path, a query.
        """
        event_type = event.get("type")
        if event_type == "tool_execution_start":
            phase = "start"
        elif event_type == "tool_execution_end":
            phase = "end"
        else:
            return None

        name = event.get("toolName")
        if not isinstance(name, str) or not name:
            return None
        call_id = event.get("toolCallId")
        detail = ""
        args = event.get("args")
        if isinstance(args, dict):
            for key in ("command", "path", "pattern", "url", "query"):
                value = args.get(key)
                if isinstance(value, str) and value:
                    detail = value
                    break
        return (
            phase,
            call_id if isinstance(call_id, str) and call_id else name,
            name,
            detail,
            bool(event.get("isError")),
        )

    @staticmethod
    async def _emit_tool_event(callback: ToolEventCallback | None, scope: str, call_id: str,
                               name: str, phase: str, duration: float | None, error: bool, trace_id: str,
                               *, summary: str | None = None) -> None:
        if callback is None:
            return
        safe_name = name if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", name) else "other"
        fallback = _TOOL_DESCRIPTIONS.get(safe_name, "搜索资料" if "search" in safe_name.lower() else "调用工具")
        summary = sanitize_tool_summary(summary) or fallback
        try:
            await callback({"call_id": f"{scope}:{call_id}", "name": safe_name, "phase": phase,
                            "summary": summary, "duration_seconds": duration, "is_error": error})
        except Exception:
            logger.warning("tool activity callback failed", extra={"trace_id": trace_id, "event": "pi.activity_error"})

    @staticmethod
    async def _emit_progress(
        on_progress: ProgressCallback | None, label: str | None, trace_id: str
    ) -> None:
        """A progress hint must never take down the turn it describes."""
        if on_progress is None:
            return
        try:
            await on_progress(label)
        except Exception:
            logger.warning(
                "progress callback failed",
                extra={"trace_id": trace_id, "event": "pi.progress_error"},
                exc_info=True,
            )

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

    def _build_native_prompt(
        self, messages: list[dict[str, str]], *, include_preamble: bool, skill_summary: str | None = None
    ) -> str:
        user_text = ""
        for message in reversed(messages):
            if str(message.get("role", "user")) == "user":
                user_text = str(message.get("content", "")).strip()
                break

        lines: list[str] = []
        if include_preamble:
            # Rules and memory ride `--append-system-prompt`; only the skill
            # summary still needs prompt injection. Async callers pass the
            # summary prebuilt off the event loop.
            summary = skill_summary if skill_summary is not None else build_skill_summary()
            if summary:
                # Anchor the skills root so `<skill-dir>` placeholders inside
                # SKILL.md files resolve to the repo copy, not a global one.
                skill_root = SKILL_ROOTS[0] if SKILL_ROOTS else "skills/"
                lines.extend(
                    [
                        "本机可用 skills 如下。若用户询问 skills，必须基于此列表回答，不要说当前环境没有加载 skill。",
                        f"技能根目录: {skill_root}，各 SKILL.md 中的 <skill-dir> 指 {skill_root}/<name>。",
                        summary,
                        "",
                    ]
                )
        lines.append(f"用户: {user_text}")
        return "\n".join(lines)

    async def _build_native_prompt_async(
        self, messages: list[dict[str, str]], *, include_preamble: bool
    ) -> str:
        # build_skill_summary walks the whole skills tree; on a large tree a
        # sync call here stalls every concurrent session, scheduler and healthz.
        summary = await asyncio.to_thread(build_skill_summary) if include_preamble else None
        return self._build_native_prompt(messages, include_preamble=include_preamble, skill_summary=summary)

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
            self._record_session_history(session_key, session_id)
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
            self._record_session_history(session_key, new_id)

    @property
    def session_history_path(self) -> str:
        return os.path.join(os.path.dirname(self._session_store_path), SESSION_HISTORY_FILENAME)

    def _record_session_history(self, session_key: str, session_id: str, *, event: str | None = None) -> bool:
        try:
            os.makedirs(os.path.dirname(self.session_history_path), exist_ok=True)
            record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session_key": session_key,
                      "session_id": session_id}
            if event:
                record["event"] = event
            with open(self.session_history_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except OSError:
            logger.warning(
                "failed to append pi session history",
                extra={"event": "pi.session_history", "backend": self._name},
            )
            return False

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
