from types import SimpleNamespace

import pytest

from core.agent.claude_cli import ClaudeCliClient


def make_settings(tmp_path):
    return SimpleNamespace(codex_work_dir=str(tmp_path), codex_timeout_seconds=30.0)


def test_claude_family_clients_use_backend_scoped_work_dirs(tmp_path) -> None:
    claude = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="claude",
        bin_path="claude",
        model="",
        permission_mode="auto",
    )
    qodercli = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="qodercli",
        bin_path="qodercli",
        model="",
        permission_mode="dangerously-skip-permissions",
        use_verbose=False,
        use_partial_messages=False,
    )

    claude_command = claude._build_command("hello", streaming=True)
    qoder_command = qodercli._build_command("hello", streaming=True)

    assert str(tmp_path / "claude") in claude_command
    assert str(tmp_path / "qodercli") in qoder_command
    assert "--verbose" in claude_command
    assert "--include-partial-messages" in claude_command
    assert "--verbose" not in qoder_command
    assert "--include-partial-messages" not in qoder_command


def test_claude_family_clients_accept_backend_timeout(tmp_path) -> None:
    client = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="claude",
        bin_path="claude",
        model="",
        permission_mode="auto",
        timeout_seconds=12.5,
    )

    assert client._timeout_seconds == 12.5


def test_claude_stream_chunks_preserve_markdown_table_boundaries(tmp_path) -> None:
    client = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="claude",
        bin_path="claude",
        model="",
        permission_mode="auto",
    )

    chunks = client._split_chunks("\n| 日期 | 收盘 |\n| --- | --- |\n")

    assert "".join(chunks) == "\n| 日期 | 收盘 |\n| --- | --- |\n"


def test_claude_prompt_includes_local_skill_summary(tmp_path, monkeypatch) -> None:
    skill_dir = tmp_path / "skills" / "yfinance"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: yfinance
description: >-
  查询全球股票行情、财务报表、K线下载等。
  当用户问到股票价格时触发。
---

# yfinance
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(ClaudeCliClient, "SKILL_ROOTS", (str(tmp_path / "skills"),))
    client = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="claude",
        bin_path="claude",
        model="",
        permission_mode="auto",
    )

    prompt = client._build_prompt([{"role": "user", "content": "列出你的所有可用 skills"}])

    assert "本机可用 skills" in prompt
    assert "`yfinance`" in prompt
    assert "查询全球股票行情" in prompt
    assert "不要说当前环境没有加载 skill" in prompt


def test_claude_prompt_leads_with_the_clock(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ClaudeCliClient, "SKILL_ROOTS", ())
    client = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="qodercli",
        bin_path="qodercli",
        model="",
        permission_mode="auto",
    )

    prompt = client._build_prompt([{"role": "user", "content": "今天几号"}])

    # rules/AGENTS.md 一直告诉模型"每轮都会注入时间"，而这个后端此前根本没注入
    assert prompt.startswith("当前系统时间: ")
    assert "今天几号" in prompt


def test_read_skill_metadata_supports_quoted_description(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "lark-im"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        """---
name: lark-im
description: "飞书即时通讯：收发消息和管理群聊。"
---
""",
        encoding="utf-8",
    )

    name, description = ClaudeCliClient._read_skill_metadata(str(skill_path))

    assert name == "lark-im"
    assert description == "飞书即时通讯：收发消息和管理群聊。"


@pytest.mark.asyncio
async def test_readline_before_deadline_times_out(tmp_path) -> None:
    import asyncio
    import time

    client = ClaudeCliClient(
        settings=make_settings(tmp_path),
        name="claude",
        bin_path="claude",
        model="",
        permission_mode="auto",
    )
    reader = asyncio.StreamReader()

    with pytest.raises(asyncio.TimeoutError):
        await client._readline_before_deadline(reader, deadline=time.monotonic() - 1)
