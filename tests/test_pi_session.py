"""Unit tests for the pi backend client.

Event fixtures mirror what `pi --mode json` actually emits (captured on v0.83.0,
shape re-verified against v0.84.2), including the two traps it has: `message_end`
fires for the user turn as well as the assistant one, and the process exits 0
even when the provider rejects the request.

v0.84.0 dropped the cumulative `message` field from `message_update`; these
fixtures never carried it, because the client only reads
`assistantMessageEvent.text_delta.delta` plus the authoritative `message_end`.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.agent.pi_cli import PiCliClient
from core.agent.types import AgentClientCancelled, AgentClientError


def _make_settings(tmp_path, **overrides):
    values = dict(
        pi_cli_bin="pi",
        pi_model="",
        pi_thinking="",
        pi_tools="",
        pi_agent_dir="",
        pi_api_key="",
        pi_offline=True,
        pi_approve_project=True,
        pi_timeout_seconds=300.0,
        pi_idle_timeout_seconds=120.0,
        pi_session_store_path=str(tmp_path / "server" / "pi-sessions.json"),
        pi_work_dir=str(tmp_path / "workdir"),
        pi_stream_read_limit_bytes=262144,
        pi_max_retries=2,
        pi_retry_backoff_seconds=0.0,
        pi_circuit_breaker_threshold=5,
        pi_circuit_breaker_cooldown_seconds=30,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeProcess:
    def __init__(self, stdout: bytes, *, return_code: int = 0, stderr: bytes = b"") -> None:
        self.pid = 424244
        self.returncode = None
        self._return_code = return_code
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        if stderr:
            self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code


def _encode(events: list[dict]) -> bytes:
    return "".join(f"{json.dumps(event, ensure_ascii=False)}\n" for event in events).encode()


async def _collect(client: PiCliClient, payload: bytes, *, return_code: int = 0, trace_id: str = "t1") -> str:
    async def spawn(*_command, **_kwargs):
        return _FakeProcess(payload, return_code=return_code)

    with patch("asyncio.create_subprocess_exec", new=spawn):
        pieces = []
        async for piece in client._run_stream_once(prompt="hi", trace_id=trace_id):
            pieces.append(piece)
        return "".join(pieces)


def test_build_command_shape(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path, pi_model="bailian/deepseek-v4-flash-0731"))

    command = client._build_command("abc123")
    assert command[:3] == ["pi", "--mode", "json"]
    assert command[command.index("--session-id") + 1] == "abc123"
    assert command[command.index("--model") + 1] == "bailian/deepseek-v4-flash-0731"
    assert "--approve" in command
    # Empty knobs must not turn into bare flags; --tools especially is an
    # allowlist that would silently disable every other tool.
    assert "--thinking" not in command
    assert "--tools" not in command

    assert "--session-id" not in client._build_command(None)


def test_optional_flags_are_passed_when_configured(tmp_path) -> None:
    client = PiCliClient(
        settings=_make_settings(
            tmp_path, pi_thinking="high", pi_tools="read,grep", pi_approve_project=False
        )
    )

    command = client._build_command("abc123")
    assert command[command.index("--thinking") + 1] == "high"
    assert command[command.index("--tools") + 1] == "read,grep"
    assert "--approve" not in command


def test_session_id_is_generated_once_and_reused(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    first, is_new_first = client._get_or_create_session_id("ou_a:oc_b")
    second, is_new_second = client._get_or_create_session_id("ou_a:oc_b")

    assert first and first == second
    assert is_new_first is True
    # The preamble (skill summary) rides is_new, so a reused session must report
    # False or the summary would be re-injected on every turn.
    assert is_new_second is False

    with open(client._session_store_path, encoding="utf-8") as fh:
        assert json.load(fh) == {"ou_a:oc_b": first}

    other, _ = client._get_or_create_session_id("ou_a:oc_other")
    assert other != first


def test_reset_session_yields_a_fresh_id(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    first, _ = client._get_or_create_session_id("ou_a:oc_b")
    client.reset_session("ou_a:oc_b")
    second, is_new = client._get_or_create_session_id("ou_a:oc_b")

    assert second != first
    assert is_new is True


def test_sessions_survive_a_restart(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    first, _ = PiCliClient(settings=settings)._get_or_create_session_id("ou_a:oc_b")

    reloaded, is_new = PiCliClient(settings=settings)._get_or_create_session_id("ou_a:oc_b")
    assert reloaded == first
    assert is_new is False


def test_ephemeral_calls_have_no_session(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    session_id, is_new = client._get_or_create_session_id(None)
    assert session_id is None
    assert is_new is True


def test_text_delta_extraction_ignores_thinking_and_toolcalls(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    assert (
        client._extract_text_delta(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "你好"}}
        )
        == "你好"
    )
    assert (
        client._extract_text_delta(
            {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "推理"}}
        )
        == ""
    )
    assert (
        client._extract_text_delta(
            {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_delta", "delta": "{"}}
        )
        == ""
    )
    assert client._extract_text_delta({"type": "turn_end", "message": {}}) == ""


def test_final_text_only_comes_from_the_assistant_message_end(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    user_end = {
        "type": "message_end",
        "message": {"role": "user", "content": [{"type": "text", "text": "问题原文"}]},
    }
    assistant_end = {
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "回答"}], "stopReason": "stop"},
    }

    # Echoing the user's own message_end back would replay the question as the reply.
    assert client._extract_final_text(user_end) == ""
    assert client._extract_final_text(assistant_end) == "回答"


def test_session_id_is_only_read_from_the_session_header(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    assert client._extract_session_id({"type": "session", "id": "abc"}) == "abc"
    assert client._extract_session_id({"type": "agent_start", "id": "abc"}) == ""


@pytest.mark.asyncio
async def test_deltas_are_streamed_without_duplicating_the_final_message(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))
    payload = _encode(
        [
            {"type": "session", "version": 3, "id": "s1"},
            {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "你好"}},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "世界"}},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "你好世界"}],
                    "stopReason": "stop",
                },
            },
            {"type": "agent_settled"},
        ]
    )

    assert await _collect(client, payload) == "你好世界"


@pytest.mark.asyncio
async def test_message_end_backfills_a_reply_when_no_deltas_arrive(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))
    payload = _encode(
        [
            {"type": "session", "version": 3, "id": "s1"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "只有终态"}],
                    "stopReason": "stop",
                },
            },
            {"type": "agent_settled"},
        ]
    )

    assert await _collect(client, payload) == "只有终态"


@pytest.mark.asyncio
async def test_provider_error_raises_even_though_the_process_exits_zero(tmp_path) -> None:
    """The whole reason error handling cannot lean on the exit code."""
    client = PiCliClient(settings=_make_settings(tmp_path))
    payload = _encode(
        [
            {"type": "session", "version": 3, "id": "s1"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": '401: {"code":"invalid_api_key"}',
                },
            },
            {"type": "agent_settled"},
        ]
    )

    with pytest.raises(AgentClientError) as excinfo:
        await _collect(client, payload)
    assert "invalid_api_key" in str(excinfo.value)


@pytest.mark.asyncio
async def test_error_after_partial_text_still_raises(tmp_path) -> None:
    """A truncated answer must not be delivered as if it were complete."""
    client = PiCliClient(settings=_make_settings(tmp_path))
    payload = _encode(
        [
            {"type": "session", "version": 3, "id": "s1"},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "先说一半"}},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "先说一半"}],
                    "stopReason": "error",
                    "errorMessage": "503 upstream overloaded",
                },
            },
            {"type": "agent_settled"},
        ]
    )

    with pytest.raises(AgentClientError) as excinfo:
        await _collect(client, payload)
    assert "upstream overloaded" in str(excinfo.value)


@pytest.mark.asyncio
async def test_intermediate_failure_followed_by_a_retry_success_is_not_an_error(tmp_path) -> None:
    """pi retries inside one process; only the last assistant turn is the verdict."""
    client = PiCliClient(settings=_make_settings(tmp_path))
    payload = _encode(
        [
            {"type": "session", "version": 3, "id": "s1"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "503 upstream overloaded",
                },
            },
            {"type": "agent_end", "willRetry": True},
            {"type": "auto_retry_start", "attempt": 2},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "重试成功"}},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "重试成功"}],
                    "stopReason": "stop",
                },
            },
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
    )

    assert await _collect(client, payload) == "重试成功"


@pytest.mark.asyncio
async def test_auth_errors_are_not_retried(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    assert client._should_retry(AgentClientError('pi cli error: 401 {"code":"invalid_api_key"}')) is False
    assert client._should_retry(AgentClientError("pi cli error: 503 InternalError")) is True


@pytest.mark.asyncio
async def test_empty_stream_raises_instead_of_replying_blank(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    with pytest.raises(AgentClientError):
        await _collect(client, b"")


@pytest.mark.asyncio
async def test_unknown_event_types_are_ignored(tmp_path) -> None:
    """pi ships breaking changes on a weekly cadence; new events must not throw."""
    client = PiCliClient(settings=_make_settings(tmp_path))
    payload = _encode(
        [
            {"type": "session", "version": 3, "id": "s1"},
            {"type": "some_future_event", "payload": {"anything": True}},
            {"type": "queue_update", "queued": 0},
            {"type": "compaction_start"},
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "ok"}},
            {"type": "compaction_end"},
            {"type": "agent_settled"},
        ]
    )

    assert await _collect(client, payload) == "ok"


@pytest.mark.asyncio
async def test_nonzero_exit_code_still_raises(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    with pytest.raises(AgentClientError) as excinfo:
        await _collect(client, _encode([{"type": "session", "id": "s1"}]), return_code=1)
    assert "return_code=1" in str(excinfo.value)


def test_system_prompt_files_carry_rules_and_memory(tmp_path) -> None:
    """Rules and memory reach pi through --append-system-prompt.

    pi 0.84.2's --append-system-prompt reads a path's contents, which is what
    lets memory keep riding a per-turn file instead of a first-turn preamble.
    """
    memory_path = str(tmp_path / "memory-context.md")

    with patch("core.agent.pi_cli.memory.write_context_file", return_value=memory_path):
        paths = PiCliClient._system_prompt_files()

    assert any(path.endswith("rules/system.md") for path in paths)
    assert paths[-1] == memory_path


def test_public_rules_file_exists_at_the_renamed_path() -> None:
    """改名防护：rules/system.md 缺失时 pi_cli 会静默跳过规则注入。"""
    root = Path(__file__).resolve().parents[1]
    assert (root / "rules" / "system.md").is_file()


def test_missing_public_rules_warns_but_does_not_raise(tmp_path, caplog) -> None:
    with (
        patch("core.agent.pi_cli._PROJECT_ROOT", str(tmp_path)),
        patch("core.agent.pi_cli.memory.write_context_file", return_value=""),
    ):
        with caplog.at_level("WARNING", logger="core.agent.pi_cli"):
            paths = PiCliClient._system_prompt_files()

    assert not any(path.endswith("rules/system.md") for path in paths)
    assert any("rules/system.md is missing" in record.message for record in caplog.records)


def test_memory_failure_never_breaks_a_turn(tmp_path) -> None:
    with patch("core.agent.pi_cli.memory.write_context_file", side_effect=RuntimeError("boom")):
        paths = PiCliClient._system_prompt_files()

    assert all(not path.endswith("memory-context.md") for path in paths)


def test_native_prompt_carries_only_the_latest_message(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))
    messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "最新问题"},
    ]

    with patch("core.agent.pi_cli.build_skill_summary", return_value=""):
        prompt = client._build_native_prompt(messages, include_preamble=False)

    assert "最新问题" in prompt
    assert "旧问题" not in prompt
    # pi persists user text into its own transcript, so a prompt-inline clock
    # would leave one stale timestamp behind per turn.
    assert "当前系统时间" not in prompt


@pytest.mark.asyncio
async def test_ephemeral_turn_uses_the_same_latest_message_prompt(tmp_path, monkeypatch) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))
    captured = {}

    async def run(**kwargs):
        captured.update(kwargs)
        return "回答"

    monkeypatch.setattr(client, "_run_once", run)
    with patch("core.agent.pi_cli.build_skill_summary", return_value=""):
        await client.chat([
            {"role": "user", "content": "旧问题"},
            {"role": "user", "content": "最新问题"},
        ], trace_id="ephemeral")

    assert captured["session_id"] is None
    assert captured["prompt"] == "用户: 最新问题"
    assert "当前系统时间" not in captured["prompt"]


def test_clock_rides_the_system_prompt_after_rules_and_memory(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    with patch.object(
        PiCliClient,
        "_system_prompt_files",
        return_value=["rules/system.md", "memory-context.md"],
    ):
        command = client._build_command(session_id="abc123")

    injected = [
        command[i + 1] for i, arg in enumerate(command) if arg == "--append-system-prompt"
    ]

    assert injected[:2] == ["rules/system.md", "memory-context.md"]
    # The clock is passed as text, generated fresh for this turn's process.
    assert injected[-1].startswith("当前系统时间: ")


def test_skill_summary_is_injected_only_on_the_first_turn(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))
    messages = [{"role": "user", "content": "最新问题"}]

    with patch("core.agent.pi_cli.build_skill_summary", return_value="- demo-skill"):
        first = client._build_native_prompt(messages, include_preamble=True)
        later = client._build_native_prompt(messages, include_preamble=False)

    assert "demo-skill" in first
    assert "demo-skill" not in later


@pytest.mark.asyncio
async def test_spawn_env_sets_key_and_offline_but_not_pwd(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path, pi_api_key="sk-unit"))
    captured = {}

    async def spawn(*command, **kwargs):
        captured.update(kwargs)
        return _FakeProcess(b"")

    with patch("asyncio.create_subprocess_exec", new=spawn):
        await client._spawn_process(["pi", "--mode", "json"])

    env = captured["env"]
    assert env["DASHSCOPE_API_KEY"] == "sk-unit"
    assert env["PI_OFFLINE"] == "1"
    # Pi binds sessions to the configured cwd directly.
    assert captured["cwd"] == str(tmp_path / "workdir")
    assert captured["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["start_new_session"] is True


@pytest.mark.parametrize("new_turn_started", [False, True])
def test_reset_during_a_turn_cannot_restore_old_session(tmp_path, new_turn_started):
    client = PiCliClient(settings=_make_settings(tmp_path))
    key = "user:chat"
    old_id, _ = client._get_or_create_session_id(key)
    client.reset_session(key)
    expected = {}
    if new_turn_started:
        fresh_id, _ = client._get_or_create_session_id(key)
        expected[key] = fresh_id
    client._persist_session(key, {"id": old_id}, expected_id=old_id)
    assert client._session_ids == expected
    assert PiCliClient(settings=_make_settings(tmp_path))._session_ids == expected


@pytest.mark.asyncio
async def test_settled_without_an_answer_is_not_success(tmp_path):
    client = PiCliClient(settings=_make_settings(tmp_path))
    with pytest.raises(AgentClientError, match="no output"):
        await _collect(client, _encode([{"type": "agent_settled"}]))


class _HangingProcess:
    """An isolated process double; never signals a real PID."""

    def __init__(self):
        self.pid = 424245
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.exited = asyncio.Event()

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("eof", [False, True])
async def test_total_timeout_reaps_process_even_after_stdout_eof(tmp_path, monkeypatch, eof):
    client = PiCliClient(settings=_make_settings(tmp_path, pi_timeout_seconds=0.02))
    process = _HangingProcess()
    if eof:
        process.stdout.feed_eof()

    async def spawn(_command):
        return process

    monkeypatch.setattr(client, "_spawn_process", spawn)
    monkeypatch.setattr(client, "_kill_process_group", lambda p: p.kill())
    with pytest.raises(AgentClientError, match="timeout"):
        async for _ in client._run_stream_once("hi", "timeout"):
            pass
    assert process.returncode == -9
    assert client._active_processes == {}


@pytest.mark.asyncio
async def test_continuous_events_do_not_extend_total_timeout(tmp_path, monkeypatch):
    client = PiCliClient(settings=_make_settings(
        tmp_path, pi_timeout_seconds=0.05, pi_idle_timeout_seconds=1.0
    ))
    process = _HangingProcess()
    events_sent = []

    async def spawn(_command):
        return process

    async def produce():
        while process.returncode is None:
            process.stdout.feed_data(b'{"type":"tool_execution_update"}\n')
            events_sent.append(1)
            await asyncio.sleep(0.002)

    monkeypatch.setattr(client, "_spawn_process", spawn)
    monkeypatch.setattr(client, "_kill_process_group", lambda p: p.kill())
    producer = asyncio.create_task(produce())
    try:
        with pytest.raises(AgentClientError, match="timeout"):
            await asyncio.wait_for(client._run_once("hi", "continuous"), timeout=2)
        assert len(events_sent) > 1
        assert process.returncode == -9
        assert client._active_processes == {}
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_timeout_remains_independent_of_total_timeout(tmp_path, monkeypatch):
    client = PiCliClient(settings=_make_settings(
        tmp_path, pi_timeout_seconds=10.0, pi_idle_timeout_seconds=0.01
    ))
    process = _HangingProcess()

    async def spawn(_command):
        return process

    monkeypatch.setattr(client, "_spawn_process", spawn)
    monkeypatch.setattr(client, "_kill_process_group", lambda p: p.kill())
    with pytest.raises(AgentClientError, match="timeout"):
        await asyncio.wait_for(client._run_once("hi", "idle"), timeout=2)
    assert process.returncode == -9
    assert client._active_processes == {}


@pytest.mark.asyncio
async def test_task_cancellation_reaps_process(tmp_path, monkeypatch):
    client = PiCliClient(settings=_make_settings(tmp_path))
    process = _HangingProcess()
    spawned = asyncio.Event()

    async def spawn(_command):
        spawned.set()
        return process

    monkeypatch.setattr(client, "_spawn_process", spawn)
    monkeypatch.setattr(client, "_kill_process_group", lambda p: p.kill())
    task = asyncio.create_task(client._run_once("hi", "cancel"))
    await spawned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9
    assert client._active_processes == {}


@pytest.mark.asyncio
async def test_close_terminates_active_processes(tmp_path, monkeypatch):
    client = PiCliClient(settings=_make_settings(tmp_path))
    process = _HangingProcess()
    client._register_process("close", process)
    monkeypatch.setattr(client, "_kill_process_group", lambda p: p.kill())
    await client.close()
    assert process.returncode == -9
    with pytest.raises(AgentClientCancelled):
        client._raise_if_cancelled("close")


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_partial_output(tmp_path, monkeypatch):
    client = PiCliClient(settings=_make_settings(tmp_path))
    attempts = []

    async def stream(**_kwargs):
        attempts.append(1)
        yield "partial"
        raise AgentClientError("503 interrupted")

    monkeypatch.setattr(client, "_run_stream_once", stream)
    pieces = []
    with pytest.raises(AgentClientError):
        async for piece in client.chat_stream([{"role": "user", "content": "hi"}], "partial"):
            pieces.append(piece)
    assert pieces == ["partial"]
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_stop_kills_and_reaps_a_real_subprocess(tmp_path, monkeypatch):
    import sys

    client = PiCliClient(settings=_make_settings(tmp_path))
    spawned = asyncio.Event()
    process = None

    async def spawn(_command):
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        spawned.set()
        return process

    monkeypatch.setattr(client, "_spawn_process", spawn)
    task = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}], "real-stop"))
    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        assert client.cancel("real-stop") is True
        with pytest.raises(AgentClientCancelled):
            await asyncio.wait_for(task, timeout=5)
        assert process.returncode is not None
        assert client._active_processes == {}
        assert client.cancel("real-stop") is False
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
