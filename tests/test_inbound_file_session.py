"""Inbound files must reach the session, not just the archive directory.

Both channels used to return early after archiving, so `append_round` never ran
and the agent had no idea a file had arrived. These tests pin the replacement
behaviour: queue a notice on arrival, inject it into the next user turn, and
never wake the backend on the arrival turn itself.
"""

import os
from types import SimpleNamespace

import pytest

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

    sessions.new_session("k")

    assert sessions.take_pending_files("k") == []


# --------------------------------------------------------------------------- #
# wechat channel
# --------------------------------------------------------------------------- #


class RecordingWeChatCodex:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.calls = 0

    async def chat_stream(self, messages, trace_id: str, *, session_key: str | None = None):
        self.calls += 1
        self.messages = messages
        yield "收到"

    async def chat(self, messages, trace_id: str, *, session_key: str | None = None) -> str:
        self.calls += 1
        self.messages = messages
        return "收到"

    def cancel(self, trace_id: str) -> bool:
        return True


class ForbiddenWeChatCodex(RecordingWeChatCodex):
    async def chat_stream(self, messages, trace_id: str, *, session_key: str | None = None):
        self.calls += 1
        raise AssertionError("a file-only message must not wake the backend")
        if False:  # pragma: no cover - keeps this an async generator
            yield ""

    async def chat(self, messages, trace_id: str, *, session_key: str | None = None) -> str:
        self.calls += 1
        raise AssertionError("a file-only message must not wake the backend")


def make_wechat_handler(codex) -> tuple[WeChatWebhookHandler, SessionManager]:
    sessions = SessionManager(max_history_rounds=10)
    handler = WeChatWebhookHandler(
        settings=SimpleNamespace(
            streaming_enabled=True,
            wechat_webhook_token="",
            wechat_message_chunk_chars=1800,
        ),
        codex_client=codex,
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
    codex = ForbiddenWeChatCodex()
    handler, sessions = make_wechat_handler(codex)

    result = await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))

    assert result["code"] == 0
    assert result["replies"] == []
    assert codex.calls == 0
    assert sessions.take_pending_files(WECHAT_SESSION_KEY) == [
        "- report.pdf → /data/file/report.pdf (4096 bytes)"
    ]


async def test_wechat_notice_reaches_the_backend_on_the_next_turn() -> None:
    codex = RecordingWeChatCodex()
    handler, sessions = make_wechat_handler(codex)

    await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))
    await handler.handle_webhook(headers={}, raw_body=wechat_body("m2", text="这个文件讲了什么"))

    assert codex.calls == 1
    prompt = codex.messages[-1]["content"]
    assert FILE_NOTICE_HEADER in prompt
    assert "/data/file/report.pdf" in prompt
    assert prompt.endswith("这个文件讲了什么")
    assert sessions.take_pending_files(WECHAT_SESSION_KEY) == []


async def test_wechat_text_and_files_in_one_message_share_a_turn() -> None:
    codex = RecordingWeChatCodex()
    handler, _ = make_wechat_handler(codex)

    await handler.handle_webhook(
        headers={}, raw_body=wechat_body("m1", text="帮我看下这个", files=ARCHIVED)
    )

    assert codex.calls == 1
    prompt = codex.messages[-1]["content"]
    assert "/data/file/report.pdf" in prompt
    assert prompt.endswith("帮我看下这个")


async def test_wechat_command_turn_does_not_consume_the_notice() -> None:
    codex = RecordingWeChatCodex()
    handler, sessions = make_wechat_handler(codex)

    await handler.handle_webhook(headers={}, raw_body=wechat_body("m1", files=ARCHIVED))
    await handler.handle_webhook(headers={}, raw_body=wechat_body("m2", text="/help"))

    assert codex.calls == 0
    # /help short-circuits before the LLM turn, so the notice must survive it.
    assert len(sessions.take_pending_files(WECHAT_SESSION_KEY)) == 1


async def test_wechat_duplicate_file_message_is_ignored() -> None:
    codex = ForbiddenWeChatCodex()
    handler, sessions = make_wechat_handler(codex)

    await handler.handle_webhook(headers={}, raw_body=wechat_body("dup", files=ARCHIVED))
    await handler.handle_webhook(headers={}, raw_body=wechat_body("dup", files=ARCHIVED))

    assert len(sessions.take_pending_files(WECHAT_SESSION_KEY)) == 1


async def test_wechat_rejects_message_with_neither_text_nor_files() -> None:
    handler, _ = make_wechat_handler(ForbiddenWeChatCodex())

    with pytest.raises(Exception) as excinfo:
        await handler.handle_webhook(headers={}, raw_body=wechat_body("m1"))

    assert getattr(excinfo.value, "status_code", None) == 400


async def test_wechat_skips_malformed_file_entries() -> None:
    handler, sessions = make_wechat_handler(ForbiddenWeChatCodex())

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
    handler, sessions = make_wechat_handler(ForbiddenWeChatCodex())

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


def make_feishu_handler(tmp_path, feishu_client, codex) -> tuple[FeishuWebhookHandler, SessionManager]:
    sessions = SessionManager(max_history_rounds=10)
    handler = FeishuWebhookHandler(
        settings=SimpleNamespace(
            streaming_enabled=True,
            feishu_encrypt_key="",
            feishu_verification_token="",
            file_archive_dir=str(tmp_path / "archive"),
        ),
        feishu_client=feishu_client,
        codex_client=codex,
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
    codex = ForbiddenWeChatCodex()
    handler, sessions = make_feishu_handler(tmp_path, feishu_client, codex)

    await handler._handle_text_event(event=feishu_file_event(), trace_id="t1")

    saved = tmp_path / "archive" / "report.pdf"
    assert saved.read_bytes() == feishu_client.file_bytes
    assert feishu_client.reply_calls == [(f"已收藏\n{os.path.realpath(saved)}", "om_f1-file")]
    assert codex.calls == 0
    assert sessions.take_pending_files(FEISHU_SESSION_KEY) == [
        f"- report.pdf → {os.path.realpath(saved)} ({len(feishu_client.file_bytes)} bytes)"
    ]


async def test_feishu_notice_reaches_the_backend_on_the_next_turn(tmp_path) -> None:
    feishu_client = FileCapableFeishuClient()
    codex = RecordingWeChatCodex()
    handler, sessions = make_feishu_handler(tmp_path, feishu_client, codex)

    await handler._handle_text_event(event=feishu_file_event(), trace_id="t1")
    await handler._handle_text_event(event=feishu_text_event("这个文件讲了什么"), trace_id="t2")

    assert codex.calls == 1
    prompt = codex.messages[-1]["content"]
    assert FILE_NOTICE_HEADER in prompt
    assert str(tmp_path / "archive" / "report.pdf") in prompt
    assert prompt.endswith("这个文件讲了什么")
    assert sessions.take_pending_files(FEISHU_SESSION_KEY) == []


async def test_feishu_download_failure_queues_nothing(tmp_path) -> None:
    feishu_client = FileCapableFeishuClient()
    feishu_client.fail_download = True
    handler, sessions = make_feishu_handler(tmp_path, feishu_client, ForbiddenWeChatCodex())

    await handler._handle_text_event(event=feishu_file_event(), trace_id="t1")

    assert feishu_client.reply_calls == [("文件收藏失败，请稍后重试。", "om_f1-file-failed")]
    # A file that never landed must not be advertised to the agent.
    assert sessions.take_pending_files(FEISHU_SESSION_KEY) == []
