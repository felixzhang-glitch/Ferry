import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.codex.client import CodexClient


class FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") for line in lines]
        self._idx = 0

    async def readline(self) -> bytes:
        if self._idx >= len(self._lines):
            return b""
        value = self._lines[self._idx]
        self._idx += 1
        return value

    async def read(self) -> bytes:
        if self._idx >= len(self._lines):
            return b""
        remain = b"".join(self._lines[self._idx :])
        self._idx = len(self._lines)
        return remain


class FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str] | None = None, return_code: int = 0) -> None:
        self.stdout = FakeStream(stdout_lines)
        self.stderr = FakeStream(stderr_lines or [])
        self.returncode: int | None = None
        self._return_code = return_code

    async def wait(self) -> int:
        self.returncode = self._return_code
        return self._return_code

    def kill(self) -> None:
        self.returncode = self._return_code


@pytest.mark.asyncio
async def test_codex_streaming_mock() -> None:
    work_dir = Path("/tmp/codexclaw-test-workdir")
    work_dir.mkdir(parents=True, exist_ok=True)

    settings = SimpleNamespace(
        codex_cli_bin="codex",
        codex_work_dir=str(work_dir),
        codex_model="codex-mini-latest",
        codex_permission_mode="full",
        codex_timeout_seconds=30.0,
        codex_stream_read_limit_bytes=262144,
        codex_max_retries=1,
        codex_retry_backoff_seconds=0.01,
        codex_circuit_breaker_threshold=5,
        codex_circuit_breaker_cooldown_seconds=30,
    )

    client = CodexClient(settings=settings)

    async def fake_spawn(command: list[str]) -> FakeProcess:
        assert command[0] == "codex"
        assert "exec" in command
        assert "--json" in command
        assert "--dangerously-bypass-approvals-and-sandbox" in command
        assert "-C" in command

        events = [
            json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n",
            json.dumps({"type": "turn.started"}) + "\n",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "你好"},
                }
            )
            + "\n",
            json.dumps({"type": "turn.completed"}) + "\n",
        ]
        return FakeProcess(stdout_lines=events, return_code=0)

    client._spawn_process = fake_spawn  # type: ignore[assignment]

    chunks: list[str] = []
    async for piece in client.chat_stream(messages=[{"role": "user", "content": "hi"}], trace_id="trace-1"):
        chunks.append(piece)

    assert "".join(chunks) == "你好"
    await client.close()


@pytest.mark.asyncio
async def test_codex_streaming_includes_generated_image_event() -> None:
    work_dir = Path("/tmp/codexclaw-test-workdir")
    work_dir.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(
        codex_cli_bin="codex",
        codex_work_dir=str(work_dir),
        codex_model="",
        codex_permission_mode="full",
        codex_timeout_seconds=30.0,
        codex_stream_read_limit_bytes=262144,
        codex_max_retries=1,
        codex_retry_backoff_seconds=0.01,
        codex_circuit_breaker_threshold=5,
        codex_circuit_breaker_cooldown_seconds=30,
    )
    client = CodexClient(settings=settings)

    async def fake_spawn(command: list[str]) -> FakeProcess:
        events = [
            json.dumps({"type": "turn.started"}) + "\n",
            json.dumps({"type": "image.generated", "path": "file:///tmp/generated.png"}) + "\n",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "已生成"},
                }
            )
            + "\n",
        ]
        return FakeProcess(stdout_lines=events, return_code=0)

    client._spawn_process = fake_spawn  # type: ignore[assignment]

    chunks: list[str] = []
    async for piece in client.chat_stream(messages=[{"role": "user", "content": "draw"}], trace_id="trace-img"):
        chunks.append(piece)

    assert "已生成" in "".join(chunks)
    assert "file:///tmp/generated.png" in "".join(chunks)
    await client.close()


def test_codex_prompt_leads_with_the_clock(tmp_path) -> None:
    settings = SimpleNamespace(
        codex_cli_bin="codex",
        codex_work_dir=str(tmp_path),
        codex_model="",
        codex_permission_mode="full",
        codex_timeout_seconds=30.0,
        codex_stream_read_limit_bytes=262144,
        codex_max_retries=1,
        codex_retry_backoff_seconds=0.01,
        codex_circuit_breaker_threshold=5,
        codex_circuit_breaker_cooldown_seconds=30,
    )
    client = CodexClient(settings=settings)

    prompt = client._build_prompt([{"role": "user", "content": "今天几号"}])

    assert prompt.startswith("当前系统时间: ")
    assert "对话历史:" in prompt
