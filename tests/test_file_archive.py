import json
import os
from types import SimpleNamespace

import pytest

from channel.feishu.client import FeishuClientError
from channel.feishu.handler import FeishuWebhookHandler
from channel.feishu.models import parse_message_event
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import SessionManager
from core.session.task_registry import ActiveTaskRegistry


def _make_payload(
    message_type: str = "file",
    content: dict | None = None,
    chat_type: str = "p2p",
    mentions: list | None = None,
) -> dict:
    if content is None:
        content = {"file_key": "file_v3_test", "file_name": "report.pdf"}
    return {
        "header": {"event_type": "im.message.receive_v1", "event_id": "ev_file_1"},
        "event": {
            "message": {
                "message_type": message_type,
                "chat_type": chat_type,
                "message_id": "om_file_1",
                "chat_id": "oc_file_1",
                "content": json.dumps(content),
                "mentions": mentions or [],
            },
            "sender": {"sender_id": {"open_id": "ou_file_1"}},
        },
    }


class TestParseFileMessageEvent:
    def test_file_message(self) -> None:
        event = parse_message_event(_make_payload())

        assert event is not None
        assert event.message_type == "file"
        assert event.file_key == "file_v3_test"
        assert event.file_name == "report.pdf"
        assert event.text == "用户发送了一个文件。"

    def test_audio_message_has_no_file_name(self) -> None:
        event = parse_message_event(
            _make_payload(message_type="audio", content={"file_key": "audio_key_1"}),
        )

        assert event is not None
        assert event.message_type == "audio"
        assert event.file_key == "audio_key_1"
        assert event.file_name == ""

    def test_media_message(self) -> None:
        event = parse_message_event(
            _make_payload(
                message_type="media",
                content={"file_key": "media_key_1", "file_name": "clip.mp4"},
            ),
        )

        assert event is not None
        assert event.file_key == "media_key_1"
        assert event.file_name == "clip.mp4"

    def test_file_without_file_key_returns_none(self) -> None:
        event = parse_message_event(_make_payload(content={"file_name": "report.pdf"}))
        assert event is None

    def test_group_file_without_mention_returns_none(self) -> None:
        event = parse_message_event(
            _make_payload(chat_type="group"),
            bot_open_id="ou_bot",
            group_require_mention=True,
        )
        assert event is None


class FakeFileFeishuClient:
    def __init__(self) -> None:
        self.reply_calls: list[tuple[str, str | None]] = []
        self.send_calls: list[tuple[str, str, str | None]] = []
        self.download_calls: list[tuple[str, str]] = []
        self.file_bytes = b"%PDF-1.4 fake"
        self.content_type = "application/pdf"
        self.fail_download = False

    async def reply_markdown(
        self,
        message_id: str,
        markdown: str,
        trace_id: str,
        request_uuid: str | None = None,
    ) -> None:
        self.reply_calls.append((markdown, request_uuid))

    async def send_markdown(
        self,
        receive_id: str,
        markdown: str,
        trace_id: str,
        receive_id_type: str = "chat_id",
        request_uuid: str | None = None,
    ) -> str:
        self.send_calls.append((receive_id, markdown, request_uuid))
        return "om_sent"

    async def download_message_file(self, message_id: str, file_key: str, trace_id: str) -> tuple[bytes, str]:
        if self.fail_download:
            raise FeishuClientError("download failed")
        self.download_calls.append((message_id, file_key))
        return self.file_bytes, self.content_type


def _make_handler(tmp_path, feishu_client: FakeFileFeishuClient) -> FeishuWebhookHandler:
    settings = SimpleNamespace(
        streaming_enabled=True,
        feishu_encrypt_key="",
        feishu_verification_token="",
        file_archive_dir=str(tmp_path / "archive"),
    )
    return FeishuWebhookHandler(
        settings=settings,
        feishu_client=feishu_client,
        # File flow must never reach the backend; None makes any call blow up.
        agent_client=None,
        session_manager=SessionManager(),
        deduplicator=MessageDeduplicator(ttl_seconds=3600),
        task_registry=ActiveTaskRegistry(),
    )


def _make_file_event(
    message_id: str = "om_file_1",
    file_key: str = "file_v3_test",
    file_name: str = "report.pdf",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        user_id="ou_file_1",
        chat_id="oc_file_1",
        text="用户发送了一个文件。",
        message_type="file",
        file_key=file_key,
        file_name=file_name,
    )


@pytest.mark.asyncio
async def test_handle_file_event_archives_and_replies(tmp_path) -> None:
    feishu_client = FakeFileFeishuClient()
    handler = _make_handler(tmp_path, feishu_client)

    await handler._handle_text_event(event=_make_file_event(), trace_id="trace_file")

    assert feishu_client.download_calls == [("om_file_1", "file_v3_test")]
    saved = tmp_path / "archive" / "report.pdf"
    assert saved.read_bytes() == feishu_client.file_bytes
    assert feishu_client.reply_calls == [
        (f"已收藏\n{os.path.realpath(saved)}", "om_file_1-file"),
    ]


@pytest.mark.asyncio
async def test_handle_file_event_duplicate_name_appends_counter(tmp_path) -> None:
    feishu_client = FakeFileFeishuClient()
    handler = _make_handler(tmp_path, feishu_client)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "report.pdf").write_bytes(b"existing")

    await handler._handle_text_event(event=_make_file_event(), trace_id="trace_file")

    saved = archive_dir / "report-1.pdf"
    assert saved.read_bytes() == feishu_client.file_bytes
    assert (archive_dir / "report.pdf").read_bytes() == b"existing"
    assert feishu_client.reply_calls[-1][0] == f"已收藏\n{os.path.realpath(saved)}"


@pytest.mark.asyncio
async def test_handle_file_event_missing_name_falls_back_to_file_key(tmp_path) -> None:
    feishu_client = FakeFileFeishuClient()
    handler = _make_handler(tmp_path, feishu_client)

    await handler._handle_text_event(
        event=_make_file_event(file_name=""),
        trace_id="trace_file",
    )

    saved = tmp_path / "archive" / "file_v3_test.pdf"
    assert saved.read_bytes() == feishu_client.file_bytes


@pytest.mark.asyncio
async def test_handle_file_event_download_failure_replies_error(tmp_path) -> None:
    feishu_client = FakeFileFeishuClient()
    feishu_client.fail_download = True
    handler = _make_handler(tmp_path, feishu_client)

    await handler._handle_text_event(event=_make_file_event(), trace_id="trace_file")

    assert not (tmp_path / "archive").exists()
    assert feishu_client.reply_calls == [
        ("文件收藏失败，请稍后重试。", "om_file_1-file-failed"),
    ]
