"""Inbound files must reach the session, not just the archive directory.

Queue a notice on arrival, inject it into the next user turn, and never wake
pi on the arrival turn itself. Native pi sessions own conversation history.
"""

import os
from types import SimpleNamespace

import pytest

from app.commands import process_command
from channel.feishu.client import FeishuClientError
from channel.feishu.handler import FeishuWebhookHandler
from channel.wechat.handler import WeChatWebhookHandler
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import (
    FILE_NOTICE_HEADER,
    SessionManager,
    apply_pending_notices,
    format_file_notice,
)
from core.session.task_registry import ActiveTaskRegistry
from test_handler_single_reply import FakeFeishuClient


# --------------------------------------------------------------------------- #
# notice formatting and the queue
# --------------------------------------------------------------------------- #


def test_format_file_notice_includes_name_path_and_size() -> None:
    assert format_file_notice("季度报表.pdf", "/data/file/季度报表.pdf", 188728) == (
        "- 季度报表.pdf → /data/file/季度报表.pdf (188728 bytes)"
    )


def test_format_file_notice_falls_back_to_basename() -> None:
    assert format_file_notice("", "/data/file/report.pdf", 10) == "- report.pdf → /data/file/report.pdf (10 bytes)"


def test_format_file_notice_omits_size_when_unknown() -> None:
    assert format_file_notice("a.txt", "/data/file/a.txt") == "- a.txt → /data/file/a.txt"


def test_apply_pending_notices_is_a_noop_without_notices() -> None:
    assert apply_pending_notices("你好", []) == "你好"
    assert apply_pending_notices("你好", ["", "   "]) == "你好"


def test_apply_pending_notices_prepends_header_then_text() -> None:
    result = apply_pending_notices("帮我看看", ["- a.pdf → /data/file/a.pdf (5 bytes)"])

    lines = result.split("\n")
    assert lines[0] == FILE_NOTICE_HEADER
    assert lines[1] == "- a.pdf → /data/file/a.pdf (5 bytes)"
    assert lines[2] == ""
    assert lines[3] == "帮我看看"


def test_notice_queue_drains_exactly_once() -> None:
    sessions = SessionManager()
    sessions.note_incoming_file("k", "- a")
    sessions.note_incoming_file("k", "- b")

    assert sessions.take_pending_files("k") == ["- a", "- b"]
    assert sessions.take_pending_files("k") == []


def test_take_pending_files_on_unknown_key_returns_empty() -> None:
    assert SessionManager().take_pending_files("nobody:nowhere") == []


def test_blank_notice_is_ignored() -> None:
    sessions = SessionManager()
    sessions.note_incoming_file("k", "   ")

    assert sessions.take_pending_files("k") == []


def test_notice_queue_is_capped() -> None:
    sessions = SessionManager()
    for index in range(SessionManager.MAX_PENDING_FILES + 15):
        sessions.note_incoming_file("k", f"- f{index}")

    queued = sessions.take_pending_files("k")
    assert len(queued) == SessionManager.MAX_PENDING_FILES
    # Oldest notices are the ones dropped.
    assert queued[0] == "- f15"


def test_reset_session_clears_pending_notices() -> None:
    sessions = SessionManager()
    sessions.note_incoming_file("k", "- a")

    sessions.reset_session("k")

    assert sessions.take_pending_files("k") == []


def test_new_session_starts_without_notices() -> None:
    sessions = SessionManager()
    sessions.note_incoming_file("k", "- a")

    agent = RecordingWeChatAgent()
    result = process_command("/new", sessions, "k", agent_client=agent)
    assert result is not None and result.handled
    assert agent.reset_keys == ["k"]

    assert sessions.take_pending_files("k") == []


# --------------------------------------------------------------------------- #
# wechat channel
# --------------------------------------------------------------------------- #


class RecordingWeChatAgent:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.calls = 0
        self.session_keys: list[str | None] = []
        self.reset_keys: list[str] = []

    async def chat_stream(self, messages, trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None):
        self.calls += 1
        self.messages = messages
        self.session_keys.append(session_key)
        yield "收到"

    async def chat(self, messages, trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None) -> str:
        self.calls += 1
        self.messages = messages
        self.session_keys.append(session_key)
        return "收到"

    def cancel(self, trace_id: str) -> bool:
        return True

    def reset_session(self, session_key: str) -> None:
        self.reset_keys.append(session_key)

    async def close(self) -> None:
        pass


class ForbiddenWeChatAgent(RecordingWeChatAgent):
    async def chat_stream(self, messages, trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None):
        self.calls += 1
        raise AssertionError("a file-only message must not wake the backend")
        if False:  # pragma: no cover - keeps this an async generator
            yield ""

    async def chat(self, messages, trace_id: str, *, session_key: str | None = None, image_paths: list[str] | None = None) -> str:
        self.calls += 1
        raise AssertionError("a file-only message must not wake the backend")


def make_wechat_handler(agent) -> tuple[WeChatWebhookHandler, SessionManager]:
    sessions = SessionManager()
    handler = WeChatWebhookHandler(
        settings=SimpleNamespace(
            streaming_enabled=True,
            wechat_webhook_token="",
            wechat_message_chunk_chars=1800,
        ),
        agent_client=agent,
        session_manager=sessions,
        deduplicator=MessageDeduplicator(ttl_seconds=3600),
        task_registry=ActiveTaskRegistry(),
    )
    return handler, sessions


def wechat_body(message_id: str, text: str = "", files: list | None = None) -> bytes:
    import json

    payload = {
        "message_id": message_id,
        "account_id": "bot1",
        "user_id": "u1@im.wechat",
        "text": text,
        "context_token": "ctx1",
    }
    if files is not None:
        payload["files"] = files
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


WECHAT_SESSION_KEY = "wechat:bot1:u1@im.wechat"
ARCHIVED = [{"name": "report.pdf", "path": "/data/file/report.pdf", "size": 4096}]


async def test_wechat_file_only_message_skips_backend_and_queues_notice() -> None:
    agent = ForbiddenWeChatAgent()
    handler, sessions = make_wechat_handler(agent)

    result = await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))

    assert result["code"] == 0
    assert result["replies"] == []
    assert agent.calls == 0
    assert sessions.take_pending_files(WECHAT_SESSION_KEY) == [
        "- report.pdf → /data/file/report.pdf (4096 bytes)"
    ]


async def test_wechat_notice_reaches_the_backend_on_the_next_turn() -> None:
    agent = RecordingWeChatAgent()
    handler, sessions = make_wechat_handler(agent)

    await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))
    await handler.handle_webhook(headers={}, raw_body=wechat_body("m2", text="这个文件讲了什么"))

    assert agent.calls == 1
    prompt = agent.messages[-1]["content"]
    assert FILE_NOTICE_HEADER in prompt
    assert "/data/file/report.pdf" in prompt
    assert prompt.endswith("这个文件讲了什么")
    assert sessions.take_pending_files(WECHAT_SESSION_KEY) == []


async def test_wechat_text_and_files_in_one_message_share_a_turn() -> None:
    agent = RecordingWeChatAgent()
    handler, _ = make_wechat_handler(agent)

    await handler.handle_webhook(
        headers={}, raw_body=wechat_body("m1", text="帮我看下这个", files=ARCHIVED)
    )

    assert agent.calls == 1
    prompt = agent.messages[-1]["content"]
    assert "/data/file/report.pdf" in prompt
    assert prompt.endswith("帮我看下这个")


async def test_wechat_command_turn_does_not_consume_the_notice() -> None:
    agent = RecordingWeChatAgent()
    handler, sessions = make_wechat_handler(agent)

    await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))
    await handler.handle_webhook(headers={}, raw_body=wechat_body("m2", text="/help"))

    assert agent.calls == 0
    # /help short-circuits before the LLM turn, so the notice must survive it.
    assert len(sessions.take_pending_files(WECHAT_SESSION_KEY)) == 1


async def test_wechat_duplicate_file_message_is_ignored() -> None:
    agent = ForbiddenWeChatAgent()
    handler, sessions = make_wechat_handler(agent)

    await handler.handle_webhook(headers={}, raw_body=wechat_body("dup", files=ARCHIVED))
    await handler.handle_webhook(headers={}, raw_body=wechat_body("dup", files=ARCHIVED))

    assert len(sessions.take_pending_files(WECHAT_SESSION_KEY)) == 1


async def test_wechat_rejects_message_with_neither_text_nor_files() -> None:
    handler, _ = make_wechat_handler(ForbiddenWeChatAgent())

    with pytest.raises(Exception) as excinfo:
        await handler.handle_webhook(headers={}, raw_body=wechat_body("m1"))

    assert getattr(excinfo.value, "status_code", None) == 400


async def test_wechat_skips_malformed_file_entries() -> None:
    handler, sessions = make_wechat_handler(ForbiddenWeChatAgent())

    await handler.handle_webhook(
        headers={},
        raw_body=wechat_body(
            "m1",
            files=[
                "not-an-object",
                {"name": "no-path.pdf"},
                {"path": "/data/file/ok.pdf", "size": "not-a-number", "name": "ok.pdf"},
            ],
        ),
    )

    # The pathless entries are dropped; a bad size degrades to 0 rather than failing.
    assert sessions.take_pending_files(WECHAT_SESSION_KEY) == ["- ok.pdf → /data/file/ok.pdf (0 bytes)"]


async def test_wechat_files_without_text_are_not_treated_as_a_command() -> None:
    handler, sessions = make_wechat_handler(ForbiddenWeChatAgent())

    await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))

    assert sessions.take_pending_files(WECHAT_SESSION_KEY) != []


# --------------------------------------------------------------------------- #
# feishu channel
# --------------------------------------------------------------------------- #


class FileCapableFeishuClient(FakeFeishuClient):
    """FakeFeishuClient covers messaging; inbound files also need the download."""

    def __init__(self) -> None:
        super().__init__()
        self.download_calls: list[tuple[str, str]] = []
        self.file_bytes = b"%PDF-1.4 fake"
        self.fail_download = False

    async def download_message_file(self, message_id: str, file_key: str, trace_id: str):
        if self.fail_download:
            raise FeishuClientError("download failed")
        self.download_calls.append((message_id, file_key))
        return self.file_bytes, "application/pdf"


def make_feishu_handler(tmp_path, feishu_client, agent) -> tuple[FeishuWebhookHandler, SessionManager]:
    sessions = SessionManager()
    handler = FeishuWebhookHandler(
        settings=SimpleNamespace(
            streaming_enabled=True,
            feishu_encrypt_key="",
            feishu_verification_token="",
            file_archive_dir=str(tmp_path / "archive"),
        ),
        feishu_client=feishu_client,
        agent_client=agent,
        session_manager=sessions,
        deduplicator=MessageDeduplicator(ttl_seconds=3600),
        task_registry=ActiveTaskRegistry(),
    )
    return handler, sessions


def feishu_file_event(message_id: str = "om_f1") -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        user_id="ou_1",
        chat_id="oc_1",
        text="用户发送了一个文件。",
        message_type="file",
        file_key="file_v3_test",
        file_name="report.pdf",
    )


def feishu_text_event(text: str, message_id: str = "om_t1") -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, user_id="ou_1", chat_id="oc_1", text=text)


FEISHU_SESSION_KEY = "ou_1:oc_1"


async def test_feishu_file_event_archives_replies_and_queues_notice(tmp_path) -> None:
    feishu_client = FileCapableFeishuClient()
    agent = ForbiddenWeChatAgent()
    handler, sessions = make_feishu_handler(tmp_path, feishu_client, agent)

    await handler._handle_text_event(event=feishu_file_event(), trace_id="t1")

    saved = tmp_path / "archive" / "report.pdf"
    assert saved.read_bytes() == feishu_client.file_bytes
    assert feishu_client.reply_calls == [(f"已收藏\n{os.path.realpath(saved)}", "om_f1-file")]
    assert agent.calls == 0
    assert sessions.take_pending_files(FEISHU_SESSION_KEY) == [
        f"- report.pdf → {os.path.realpath(saved)} ({len(feishu_client.file_bytes)} bytes)"
    ]


async def test_feishu_notice_reaches_the_backend_on_the_next_turn(tmp_path) -> None:
    feishu_client = FileCapableFeishuClient()
    agent = RecordingWeChatAgent()
    handler, sessions = make_feishu_handler(tmp_path, feishu_client, agent)

    await handler._handle_text_event(event=feishu_file_event(), trace_id="t1")
    await handler._handle_text_event(event=feishu_text_event("这个文件讲了什么"), trace_id="t2")

    assert agent.calls == 1
    prompt = agent.messages[-1]["content"]
    assert FILE_NOTICE_HEADER in prompt
    assert str(tmp_path / "archive" / "report.pdf") in prompt
    assert prompt.endswith("这个文件讲了什么")
    assert sessions.take_pending_files(FEISHU_SESSION_KEY) == []


async def test_feishu_download_failure_queues_nothing(tmp_path) -> None:
    feishu_client = FileCapableFeishuClient()
    feishu_client.fail_download = True
    handler, sessions = make_feishu_handler(tmp_path, feishu_client, ForbiddenWeChatAgent())

    await handler._handle_text_event(event=feishu_file_event(), trace_id="t1")

    assert feishu_client.reply_calls == [("文件收藏失败，请稍后重试。", "om_f1-file-failed")]
    # A file that never landed must not be advertised to the agent.
    assert sessions.take_pending_files(FEISHU_SESSION_KEY) == []


@pytest.fixture(params=["feishu", "wechat"])
def channel_turn(request, tmp_path):
    agent = RecordingWeChatAgent()
    if request.param == "feishu":
        transport = FileCapableFeishuClient()
        handler, sessions = make_feishu_handler(tmp_path, transport, agent)
        key = FEISHU_SESSION_KEY

        async def send(text, message_id):
            offset = len(transport.reply_calls)
            await handler._handle_text_event(feishu_text_event(text, message_id), f"t-{message_id}")
            return [text for text, _ in transport.reply_calls[offset:]]
    else:
        handler, sessions = make_wechat_handler(agent)
        key = WECHAT_SESSION_KEY

        async def send(text, message_id):
            result = await handler.handle_webhook(headers={}, raw_body=wechat_body(message_id, text=text))
            return result["replies"]

    return handler, sessions, agent, key, send


@pytest.mark.parametrize("streaming", [False, True])
async def test_channels_send_only_current_turn_and_inject_notices_once(channel_turn, streaming):
    handler, sessions, agent, key, send = channel_turn
    handler._settings.streaming_enabled = streaming
    sessions.note_incoming_file(key, "- attachment.pdf")

    assert await send("第一轮问题", "m1") == ["收到"]
    assert agent.messages == [{
        "role": "user", "content": apply_pending_notices("第一轮问题", ["- attachment.pdf"])
    }]
    assert await send("第二轮问题", "m2") == ["收到"]
    assert agent.messages == [{"role": "user", "content": "第二轮问题"}]
    assert agent.session_keys == [key, key]
    assert sessions.take_pending_files(key) == []


async def test_registry_rejection_preserves_notices_for_next_accepted_turn(channel_turn):
    handler, sessions, agent, key, send = channel_turn
    sessions.note_incoming_file(key, "- attachment.pdf")
    assert handler._task_registry.start(key=key, trace_id="busy", message_id="busy", cancel_callback=lambda: True)

    replies = await send("被拒绝的消息", "m1")
    assert "已有任务在运行" in replies[0]
    assert agent.calls == 0
    handler._task_registry.finish(key=key, trace_id="busy")
    assert await send("下一轮", "m2") == ["收到"]
    assert agent.messages == [{"role": "user", "content": apply_pending_notices("下一轮", ["- attachment.pdf"])}]
    assert agent.session_keys == [key]
    assert sessions.take_pending_files(key) == []


async def test_queue_rejection_preserves_notices_for_next_accepted_turn(channel_turn):
    from core.session.message_queue import SessionMessageQueue

    handler, sessions, agent, key, send = channel_turn
    sessions.note_incoming_file(key, "- attachment.pdf")
    handler._message_queue = SessionMessageQueue(max_pending=0)

    replies = await send("被拒绝的消息", "m1")
    assert "排队消息已满" in replies[0]
    assert agent.calls == 0
    handler._message_queue = SessionMessageQueue(max_pending=3)
    assert await send("下一轮", "m2") == ["收到"]
    assert agent.messages == [{"role": "user", "content": apply_pending_notices("下一轮", ["- attachment.pdf"])}]
    assert agent.session_keys == [key]
    assert sessions.take_pending_files(key) == []


@pytest.mark.parametrize("command", ["/codex", "/claude", "/qodercli", "/opencode"])
@pytest.mark.parametrize("suffix", ["", " 请执行任务"])
async def test_removed_commands_never_reach_model_or_consume_files(channel_turn, command, suffix):
    _, sessions, agent, key, send = channel_turn
    sessions.note_incoming_file(key, "- attachment.pdf")

    replies = await send(command + suffix, "m1")
    assert "已移除" in replies[0]
    assert "唯一后端为 pi" in replies[0]
    assert agent.calls == 0
    assert agent.reset_keys == []
    assert sessions.take_pending_files(key) == ["- attachment.pdf"]


@pytest.mark.parametrize("command", ["/help", "/backend", "/pi", "/compact", "/compress", "/daily list", "/remind 1m 喝水"])
async def test_local_commands_do_not_wake_model_or_drain_files(channel_turn, command):
    _, sessions, agent, key, send = channel_turn
    sessions.note_incoming_file(key, "- attachment.pdf")

    assert await send(command, "m1")
    assert agent.calls == 0
    assert agent.reset_keys == []
    assert sessions.take_pending_files(key) == ["- attachment.pdf"]


async def test_skills_command_uses_shared_catalog_without_waking_model(channel_turn, tmp_path, monkeypatch):
    from app import skills

    _, sessions, agent, key, send = channel_turn
    skill_dir = tmp_path / "skills" / "unit-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: unit-skill\ndescription: isolated catalog entry\n---\n", encoding="utf-8")
    monkeypatch.setattr(skills, "SKILL_ROOTS", (str(tmp_path / "skills"),))
    sessions.note_incoming_file(key, "- attachment.pdf")

    replies = await send("/skills", "m1")
    assert "unit-skill" in "".join(replies)
    assert "isolated catalog entry" in "".join(replies)
    assert agent.calls == 0
    assert sessions.take_pending_files(key) == ["- attachment.pdf"]
