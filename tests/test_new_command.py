from unittest.mock import Mock

import pytest

from app.commands import HELP_TEXT, build_help_text, parse_reminder_command, process_command
from core.session.manager import SessionManager


@pytest.mark.parametrize("command", ["/new", "/reset", " /NEW ", " /RESET "])
def test_session_command_clears_attachments_and_resets_pi(command) -> None:
    manager = SessionManager()
    key = SessionManager.build_key("u1", "c1")
    manager.note_incoming_file(key, "pending-file")
    manager.note_incoming_file("other", "other-file")
    agent_client = Mock(spec=["reset_session"])

    result = process_command(command, manager, key, agent_client=agent_client)

    assert result is not None and result.handled
    expected = "已创建新会话，不再继承历史，待处理附件已清空。" if command.strip().lower() == "/new" else "已清空当前会话上下文及待处理附件。"
    assert result.reply_text == expected
    agent_client.reset_session.assert_called_once_with("u1:c1")
    assert manager.take_pending_files(key) == []
    assert manager.take_pending_files("other") == ["other-file"]


def test_parse_reminder_command() -> None:
    result = parse_reminder_command("/remind 10m 喝水")

    assert result is not None
    assert result.delay_seconds == 600
    assert result.text == "喝水"


@pytest.mark.parametrize("command", ["/compact", "/compress", "/compact 2"])
def test_compact_does_not_claim_to_compress_or_reset_pi(command) -> None:
    manager = SessionManager()
    manager.note_incoming_file("key", "pending-file")
    agent_client = Mock(spec=["reset_session"])

    result = process_command(command, manager, "key", agent_client=agent_client)

    assert result is not None and result.handled
    assert result.reply_text == "上下文由 pi 自动管理，桥接层不支持手工压缩。"
    agent_client.reset_session.assert_not_called()
    assert manager.take_pending_files("key") == ["pending-file"]


@pytest.mark.parametrize("command", ["/backend", "/pi", "/backend claude", "/pi model"])
def test_pi_is_the_only_backend_and_query_does_not_reset_session(command) -> None:
    manager = SessionManager()
    manager.note_incoming_file("key", "pending-file")
    agent_client = Mock(spec=["reset_session"])

    result = process_command(command, manager, "key", agent_client=agent_client)

    assert result is not None and result.handled
    assert result.reply_text == "当前唯一后端为 pi，不支持切换后端。"
    agent_client.reset_session.assert_not_called()
    assert manager.take_pending_files("key") == ["pending-file"]


@pytest.mark.parametrize("command", ["/codex", "/claude", "/qodercli", "/opencode"])
@pytest.mark.parametrize("suffix", ["", " 请执行任务"])
def test_removed_backend_commands_are_handled_without_resetting(command, suffix) -> None:
    manager = SessionManager()
    manager.note_incoming_file("key", "pending-file")
    agent_client = Mock(spec=["reset_session"])

    result = process_command(command.upper() + suffix, manager, "key", agent_client=agent_client)

    assert result is not None and result.handled
    assert "已移除" in result.reply_text
    assert "唯一后端为 pi" in result.reply_text
    agent_client.reset_session.assert_not_called()
    assert manager.take_pending_files("key") == ["pending-file"]


def test_help_is_generated_from_one_command_list() -> None:
    assert HELP_TEXT == build_help_text()
    assert "/daily" in HELP_TEXT and "/skills" in HELP_TEXT and "/stop" in HELP_TEXT
    assert "唯一后端 pi" in HELP_TEXT and "自动管理" in HELP_TEXT
    assert all(command not in HELP_TEXT for command in ("/codex", "/claude", "/qodercli", "/opencode"))
    assert "微信渠道暂不支持" in build_help_text(include_remind=False)
    assert process_command("/help", SessionManager(), "key").reply_text == HELP_TEXT


def test_skills_and_plain_text_remain_handled_by_channels() -> None:
    manager = SessionManager()
    assert process_command("/skills", manager, "key") is None
    assert process_command("hello", manager, "key") is None
