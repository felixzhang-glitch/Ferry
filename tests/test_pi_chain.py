"""End-to-end chain tests: channel handler -> PiCliClient -> pi CLI.

The pi subprocess is faked at the `create_subprocess_exec` boundary and replays
the exact JSONL shape observed from `pi --mode json` (captured on v0.83.0, shape
re-verified against v0.84.2, provider bailian/deepseek-v4-flash-0731), so
everything above the process boundary is the real code path: command building,
system-prompt file injection, delta parsing, session id ownership and
persistence and the channel reply.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import Settings
from channel.feishu.handler import FeishuWebhookHandler
from channel.wechat.handler import WeChatWebhookHandler
from core.agent.pi_cli import PiCliClient
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import SessionManager
from core.session.task_registry import ActiveTaskRegistry

MODEL = "bailian/deepseek-v4-flash-0731"


def _make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "PI_WORK_DIR": str(tmp_path / "workdir"),
        "GENERATED_IMAGES_DIR": str(tmp_path / "generated-images"),
        "FEISHU_RECEIVED_IMAGES_DIR": str(tmp_path / "received-images"),
        "PI_MODEL": MODEL,
        "PI_SESSION_STORE_PATH": str(tmp_path / "server" / "pi-sessions.json"),
        "DASHSCOPE_API_KEY": "sk-test-chain",
        "PI_MAX_RETRIES": 0,
        "PI_CLI_BIN": "pi",
        "PI_OFFLINE": True,
        "PI_APPROVE_PROJECT": True,
        "MEMORY_ENABLED": False,
        "MEMORY_DIR": str(tmp_path / "memory"),
        "MEMORY_GIT_DIR": str(tmp_path / "memory-git"),
        "MEMORY_CONTEXT_PATH": str(tmp_path / "server" / "memory-context.md"),
        "STREAMING_ENABLED": True,
        "WECHAT_WEBHOOK_TOKEN": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _assistant_message(reply: str, *, stop_reason: str = "stop") -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": reply}],
        "api": "openai-completions",
        "provider": "bailian",
        "model": "deepseek-v4-flash-0731",
        "usage": {"input": 1542, "output": 4, "totalTokens": 1546},
        "stopReason": stop_reason,
        "timestamp": 1785826849386,
        "responseId": "chatcmpl-chain0001",
    }


def _jsonl(
    reply: str,
    *,
    session_id: str,
    thinking: str | None = None,
    prompt: str = "问题",
) -> bytes:
    """Replays a real `pi --mode json` turn, deltas included."""
    events: list[dict] = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-08-04T07:00:49.253Z",
            "cwd": "/tmp/pi",
        },
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        # pi emits message_end for the user turn too; only the assistant one counts.
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        {"type": "message_start", "message": _assistant_message("", stop_reason="pending")},
    ]
    if thinking is not None:
        events.append(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
            }
        )
        events.append(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_delta",
                    "contentIndex": 0,
                    "delta": thinking,
                },
            }
        )
        events.append(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0, "content": thinking},
            }
        )
    events.append(
        {"type": "message_update", "assistantMessageEvent": {"type": "text_start", "contentIndex": 0}}
    )
    # Real deltas: each event carries only the new suffix.
    for piece in (reply[: len(reply) // 2], reply[len(reply) // 2 :]):
        events.append(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": piece},
            }
        )
    events.append(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": reply},
        }
    )
    events.extend(
        [
            {"type": "message_end", "message": _assistant_message(reply)},
            {"type": "turn_end", "message": _assistant_message(reply), "toolResults": []},
            {"type": "agent_end", "messages": [], "willRetry": False},
            {"type": "agent_settled"},
        ]
    )
    return "".join(f"{json.dumps(event, ensure_ascii=False)}\n" for event in events).encode()


class _FakeProcess:
    def __init__(self, stdout: bytes, *, return_code: int = 0, stderr: bytes = b"") -> None:
        self.pid = 424243
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


class _SpawnRecorder:
    """Replaces the pi subprocess and records how it was invoked."""

    def __init__(self, *replies: str, thinking: str | None = None) -> None:
        self._replies = list(replies)
        self._thinking = thinking
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    async def __call__(self, *command, **kwargs):
        self.calls.append((command, kwargs))
        reply = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        command_list = list(command)
        session_id = (
            command_list[command_list.index("--session-id") + 1]
            if "--session-id" in command_list
            else "ephemeral"
        )
        return _FakeProcess(_jsonl(reply, session_id=session_id, thinking=self._thinking))

    def command(self, index: int = 0) -> list[str]:
        return list(self.calls[index][0])

    def env(self, index: int = 0) -> dict:
        return self.calls[index][1]["env"]

    def kwargs(self, index: int = 0) -> dict:
        return self.calls[index][1]


class FakeFeishuClient:
    def __init__(self) -> None:
        self.reply_calls: list[tuple[str, str | None]] = []
        self.send_calls: list[tuple[str, str, str | None]] = []
        self.reaction_calls: list[str] = []

    async def reply_markdown(self, message_id, markdown, trace_id, request_uuid=None) -> None:
        self.reply_calls.append((markdown, request_uuid))

    async def reply_text(self, message_id, text, trace_id, request_uuid=None) -> None:
        self.reply_calls.append((text, request_uuid))

    async def send_markdown(self, receive_id, markdown, trace_id, receive_id_type="chat_id", request_uuid=None) -> str:
        self.send_calls.append((receive_id, markdown, request_uuid))
        return "om_sent"

    async def send_text(self, receive_id, text, trace_id, receive_id_type="chat_id", request_uuid=None) -> str:
        self.send_calls.append((receive_id, text, request_uuid))
        return "om_sent"

    async def create_reaction(self, message_id, emoji_type, trace_id) -> None:
        self.reaction_calls.append(emoji_type)

    async def upload_image(self, image_path, trace_id) -> str:
        return "img_chain"

    async def reply_image(self, message_id, image_key, trace_id, request_uuid=None) -> None:
        return None

    async def send_image(self, receive_id, image_key, trace_id, receive_id_type="chat_id", request_uuid=None) -> str:
        return "om_image"


def _feishu_handler(settings: Settings, feishu_client: FakeFeishuClient, agent_client: PiCliClient) -> FeishuWebhookHandler:
    return FeishuWebhookHandler(
        settings=settings,
        feishu_client=feishu_client,
        agent_client=agent_client,
        session_manager=SessionManager(),
        deduplicator=MessageDeduplicator(ttl_seconds=3600),
        task_registry=ActiveTaskRegistry(),
    )


@pytest.mark.asyncio
async def test_feishu_message_reaches_pi_and_reply_flows_back(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    feishu_client = FakeFeishuClient()
    spawn = _SpawnRecorder("杭州今天多云，18 到 24 度。")

    with (
        patch("core.agent.pi_cli.memory.write_context_file", return_value=None),
        patch("asyncio.create_subprocess_exec", new=spawn),
    ):
        agent_client = PiCliClient(settings=settings)

        handler = _feishu_handler(settings, feishu_client, agent_client)
        await handler._handle_text_event(
            event=SimpleNamespace(
                message_id="om_pi_1",
                user_id="ou_pi_1",
                chat_id="oc_pi_1",
                text="杭州天气怎么样",
            ),
            trace_id="trace-pi-feishu",
        )

    assert feishu_client.reaction_calls == ["Typing"]
    assert [text for text, _ in feishu_client.reply_calls] == ["杭州今天多云，18 到 24 度。"]

    command = spawn.command()
    assert command[:3] == ["pi", "--mode", "json"]
    assert command[command.index("--model") + 1] == MODEL
    assert command[-1].endswith("杭州天气怎么样")
    # Rules ride --append-system-prompt; memory is disabled in this fixture.
    assert command.count("--append-system-prompt") >= 1
    assert "--approve" in command

    env = spawn.env()
    assert env["DASHSCOPE_API_KEY"] == "sk-test-chain"
    assert env["PI_OFFLINE"] == "1"

    kwargs = spawn.kwargs()
    assert kwargs["cwd"] == str(tmp_path / "workdir")
    # supervisor's stdin pipe never closes and pi would read it as prompt input.
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["start_new_session"] is True

    # Ferry owns the session id, so it is on disk before pi ever confirms it.
    session_id = command[command.index("--session-id") + 1]
    with open(settings.pi_session_store_path, encoding="utf-8") as fh:
        assert list(json.load(fh).values()) == [session_id]


@pytest.mark.asyncio
async def test_second_feishu_turn_reuses_the_stored_pi_session_id(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    feishu_client = FakeFeishuClient()
    spawn = _SpawnRecorder("第一轮回复", "第二轮回复")

    with (
        patch("core.agent.pi_cli.memory.write_context_file", return_value=None),
        patch("asyncio.create_subprocess_exec", new=spawn),
    ):
        handler = _feishu_handler(settings, feishu_client, PiCliClient(settings=settings))
        for index, text in enumerate(("第一个问题", "第二个问题"), start=1):
            await handler._handle_text_event(
                event=SimpleNamespace(
                    message_id=f"om_pi_turn_{index}",
                    user_id="ou_pi_1",
                    chat_id="oc_pi_1",
                    text=text,
                ),
                trace_id=f"trace-pi-turn-{index}",
            )

    assert [text for text, _ in feishu_client.reply_calls] == ["第一轮回复", "第二轮回复"]

    first, second = spawn.command(0), spawn.command(1)
    first_id = first[first.index("--session-id") + 1]
    second_id = second[second.index("--session-id") + 1]
    assert first_id == second_id

    # A continued session sends only the newest user message, not the history.
    assert second[-1].endswith("第二个问题")
    assert "第一个问题" not in second[-1]


@pytest.mark.asyncio
async def test_wechat_webhook_reaches_pi_through_agent_client(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    spawn = _SpawnRecorder("微信链路通了")

    with (
        patch("core.agent.pi_cli.memory.write_context_file", return_value=None),
        patch("asyncio.create_subprocess_exec", new=spawn),
    ):
        handler = WeChatWebhookHandler(
            settings=settings,
            agent_client=PiCliClient(settings=settings),
            session_manager=SessionManager(),
            deduplicator=MessageDeduplicator(ttl_seconds=3600),
            task_registry=ActiveTaskRegistry(),
        )
        result = await handler.handle_webhook(
            headers={},
            raw_body=json.dumps(
                {
                    "message_id": "wx_pi_1",
                    "account_id": "bot1",
                    "user_id": "wx_user_1",
                    "text": "链路测试",
                },
                ensure_ascii=False,
            ).encode(),
        )

    assert result["code"] == 0
    assert result["replies"] == ["微信链路通了"]
    assert spawn.command()[:3] == ["pi", "--mode", "json"]


@pytest.mark.asyncio
async def test_thinking_deltas_never_reach_either_channel(tmp_path) -> None:
    """Thinking stays out of replies on both channels.

    Both channels join whatever the backend yields, so this invariant lives in
    the pi event parser rather than in either handler -- one thinking leak would
    surface in Feishu and WeChat alike.
    """
    settings = _make_settings(tmp_path)
    feishu_client = FakeFeishuClient()
    thinking = "用户问天气。我需要先判断城市，再决定要不要调用工具，最后组织回答。"
    spawn = _SpawnRecorder("杭州今天多云，18 到 24 度。", thinking=thinking)

    with (
        patch("core.agent.pi_cli.memory.write_context_file", return_value=None),
        patch("asyncio.create_subprocess_exec", new=spawn),
    ):
        agent_client = PiCliClient(settings=settings)
        feishu = _feishu_handler(settings, feishu_client, agent_client)
        await feishu._handle_text_event(
            event=SimpleNamespace(
                message_id="om_pi_thinking",
                user_id="ou_pi_1",
                chat_id="oc_pi_1",
                text="杭州天气怎么样",
            ),
            trace_id="trace-pi-thinking",
        )

        wechat = WeChatWebhookHandler(
            settings=settings,
            agent_client=agent_client,
            session_manager=SessionManager(),
            deduplicator=MessageDeduplicator(ttl_seconds=3600),
            task_registry=ActiveTaskRegistry(),
        )
        result = await wechat.handle_webhook(
            headers={},
            raw_body=json.dumps(
                {
                    "message_id": "wx_pi_thinking",
                    "account_id": "bot1",
                    "user_id": "wx_user_1",
                    "text": "杭州天气怎么样",
                },
                ensure_ascii=False,
            ).encode(),
        )

    assert [text for text, _ in feishu_client.reply_calls] == ["杭州今天多云，18 到 24 度。"]
    assert result["replies"] == ["杭州今天多云，18 到 24 度。"]
    for delivered in [text for text, _ in feishu_client.reply_calls] + result["replies"]:
        assert thinking not in delivered
        assert "我需要先判断城市" not in delivered


@pytest.mark.asyncio
async def test_provider_error_with_exit_code_zero_replies_generic_error(tmp_path) -> None:
    """`pi --mode json` exits 0 on provider failures.

    Verified against pi 0.83.0 and re-checked on 0.84.2: an invalid API key still
    gives `EXIT=0` and the only signal is the assistant `message_end` carrying
    `stopReason: "error"`. Trusting the exit code would ship the failure to the
    user as an empty reply.
    """
    settings = _make_settings(tmp_path)
    feishu_client = FakeFeishuClient()
    detail = '401: {"message":"Incorrect API key provided.","code":"invalid_api_key"}'

    events = [
        {"type": "session", "version": 3, "id": "err0001", "cwd": "/tmp/pi"},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message_start", "message": _assistant_message("", stop_reason="pending")},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": detail,
            },
        },
        {"type": "turn_end", "message": {"role": "assistant", "content": []}, "toolResults": []},
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "agent_settled"},
    ]
    payload = "".join(f"{json.dumps(event, ensure_ascii=False)}\n" for event in events).encode()

    async def failing_spawn(*command, **kwargs):
        return _FakeProcess(payload, return_code=0)

    with (
        patch("core.agent.pi_cli.memory.write_context_file", return_value=None),
        patch("asyncio.create_subprocess_exec", new=failing_spawn),
    ):
        handler = _feishu_handler(settings, feishu_client, PiCliClient(settings=settings))
        await handler._handle_text_event(
            event=SimpleNamespace(
                message_id="om_pi_fail",
                user_id="ou_pi_1",
                chat_id="oc_pi_1",
                text="触发失败",
            ),
            trace_id="trace-pi-fail",
        )

    replied = [text for text, _ in feishu_client.reply_calls]
    # A failed backend run must still produce a user-visible reply, and the raw
    # provider error stays in the logs rather than reaching the chat.
    assert replied == ["服务繁忙，请稍后重试。"]
    assert "invalid_api_key" not in replied[-1]


@pytest.fixture(autouse=True)
def isolate_pi_context(monkeypatch):
    monkeypatch.setattr("core.agent.pi_cli.memory.write_context_file", lambda: None)
    monkeypatch.setattr("core.agent.pi_cli.build_skill_summary", lambda: "")


def _channel_harness(channel, settings, agent_client, sessions=None, deduplicator=None, task_registry=None):
    sessions = sessions if sessions is not None else SessionManager()
    deduplicator = deduplicator if deduplicator is not None else MessageDeduplicator(ttl_seconds=3600)
    task_registry = task_registry if task_registry is not None else ActiveTaskRegistry()
    if channel == "feishu":
        transport = FakeFeishuClient()
        handler = FeishuWebhookHandler(
            settings=settings,
            feishu_client=transport,
            agent_client=agent_client,
            session_manager=sessions,
            deduplicator=deduplicator,
            task_registry=task_registry,
        )
        key = "shared-user:shared-chat"

        async def send(text, message_id):
            offset = len(transport.reply_calls)
            await handler._handle_text_event(
                event=SimpleNamespace(
                    message_id=message_id, user_id="shared-user", chat_id="shared-chat", text=text
                ),
                trace_id=f"trace-{message_id}",
            )
            return [text for text, _ in transport.reply_calls[offset:]]
    else:
        handler = WeChatWebhookHandler(
            settings=settings,
            agent_client=agent_client,
            session_manager=sessions,
            deduplicator=deduplicator,
            task_registry=task_registry,
        )
        key = "wechat:shared-chat:shared-user"

        async def send(text, message_id):
            result = await handler.handle_webhook(
                headers={},
                raw_body=json.dumps({
                    "message_id": message_id, "account_id": "shared-chat", "user_id": "shared-user", "text": text
                }).encode(),
            )
            return result["replies"]

    return send, key, sessions


def _session_id(spawn, index):
    command = spawn.command(index)
    return command[command.index("--session-id") + 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["feishu", "wechat"])
@pytest.mark.parametrize("command", ["/new", "/reset"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_session_commands_cut_the_real_pi_session_and_clear_files(tmp_path, monkeypatch, channel, command, streaming):
    settings = _make_settings(tmp_path, STREAMING_ENABLED=streaming)
    spawn = _SpawnRecorder("旧会话回复", "新会话回复", "重启后续接")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    agent_client = PiCliClient(settings=settings)
    send, key, sessions = _channel_harness(channel, settings, agent_client)

    assert await send("旧问题", "m1") == ["旧会话回复"]
    old_id = _session_id(spawn, 0)
    sessions.note_incoming_file(key, "- stale-file.pdf")
    replies = await send(command, "reset-command")

    assert len(spawn.calls) == 1  # commands never reach the model
    expected = "已创建新会话，不再继承历史，待处理附件已清空。" if command == "/new" else "已清空当前会话上下文及待处理附件。"
    assert replies == [expected]
    assert sessions.take_pending_files(key) == []
    with open(settings.pi_session_store_path, encoding="utf-8") as fh:
        assert key not in json.load(fh)
    assert key not in PiCliClient(settings=settings)._session_ids

    assert await send("新问题", "m2") == ["新会话回复"]
    new_id = _session_id(spawn, 1)
    assert new_id != old_id
    assert "旧问题" not in spawn.command(1)[-1]
    assert "stale-file.pdf" not in spawn.command(1)[-1]
    await agent_client.close()

    restarted, restarted_key, _ = _channel_harness(channel, settings, PiCliClient(settings=settings))
    assert restarted_key == key
    assert await restarted("重启追问", "m3") == ["重启后续接"]
    assert _session_id(spawn, 2) == new_id
    assert spawn.kwargs(2)["cwd"] == settings.pi_work_dir


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_channels_keep_separate_native_sessions_and_resume_after_restart(tmp_path, monkeypatch, streaming):
    settings = _make_settings(tmp_path, STREAMING_ENABLED=streaming)
    spawn = _SpawnRecorder("收到")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    sessions = SessionManager()
    agent_client = PiCliClient(settings=settings)
    shared = dict(deduplicator=MessageDeduplicator(ttl_seconds=3600), task_registry=ActiveTaskRegistry())
    feishu, feishu_key, _ = _channel_harness("feishu", settings, agent_client, sessions, **shared)
    wechat, wechat_key, _ = _channel_harness("wechat", settings, agent_client, sessions, **shared)
    sessions.note_incoming_file(feishu_key, "- feishu-only.pdf")
    sessions.note_incoming_file(wechat_key, "- wechat-only.pdf")

    assert await feishu("飞书首轮", "m1") == ["收到"]
    assert await wechat("微信首轮", "m1") == ["收到"]
    first_ids = [_session_id(spawn, index) for index in range(2)]
    assert first_ids[0] != first_ids[1]
    assert "feishu-only.pdf" in spawn.command(0)[-1]
    assert "wechat-only.pdf" not in spawn.command(0)[-1]
    assert "wechat-only.pdf" in spawn.command(1)[-1]
    assert "feishu-only.pdf" not in spawn.command(1)[-1]
    with open(settings.pi_session_store_path, encoding="utf-8") as fh:
        assert json.load(fh) == dict(zip([feishu_key, wechat_key], first_ids))
    await agent_client.close()

    restarted_client = PiCliClient(settings=settings)
    fresh_sessions = SessionManager()
    shared = dict(deduplicator=MessageDeduplicator(ttl_seconds=3600), task_registry=ActiveTaskRegistry())
    feishu, _, _ = _channel_harness("feishu", settings, restarted_client, fresh_sessions, **shared)
    wechat, _, _ = _channel_harness("wechat", settings, restarted_client, fresh_sessions, **shared)
    assert await feishu("飞书追问", "m2") == ["收到"]
    assert await wechat("微信追问", "m2") == ["收到"]
    assert [_session_id(spawn, index) for index in (2, 3)] == first_ids
    assert spawn.command(2)[-1] == "用户: 飞书追问"
    assert spawn.command(3)[-1] == "用户: 微信追问"

    # Resetting one channel leaves the other channel's pi session intact.
    await feishu("/reset", "reset")
    assert await wechat("微信继续", "m3") == ["收到"]
    assert _session_id(spawn, 4) == first_ids[1]
    assert await feishu("飞书新会话", "m3") == ["收到"]
    assert _session_id(spawn, 5) not in first_ids
    await restarted_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["feishu", "wechat"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_pi_nonzero_exit_returns_safe_error(tmp_path, monkeypatch, channel, streaming):
    settings = _make_settings(tmp_path, STREAMING_ENABLED=streaming)
    calls = []

    async def failing_spawn(*command, **kwargs):
        calls.append(command)
        return _FakeProcess(b"", return_code=1, stderr=b"private provider detail")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_spawn)
    send, _, _ = _channel_harness(channel, settings, PiCliClient(settings=settings))
    assert await send("触发失败", "m1") == ["服务繁忙，请稍后重试。"]
    assert len(calls) == 1
