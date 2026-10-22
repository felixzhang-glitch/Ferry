from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from core.agent.types import AgentClient
from core.session.manager import SessionManager


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command: str
    usage: str
    description: str
    channels: frozenset[str]
    aliases: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()


_ALL_CHAT_CHANNELS = frozenset({"feishu", "wechat", "web"})
_IM_CHANNELS = frozenset({"feishu", "wechat"})
COMMAND_SPECS = (
    CommandSpec("/help", "/help", "查看可用命令", _ALL_CHAT_CHANNELS),
    CommandSpec("/new", "/new", "新建不继承历史的对话", _ALL_CHAT_CHANNELS),
    CommandSpec("/reset", "/reset", "清空当前对话上下文", _ALL_CHAT_CHANNELS),
    CommandSpec("/compact", "/compact", "说明上下文的自动管理方式", _IM_CHANNELS, ("/compress",)),
    CommandSpec("/stop", "/stop", "终止当前正在运行的任务", _ALL_CHAT_CHANNELS),
    CommandSpec("/backend", "/backend", "查看当前唯一后端 pi", _ALL_CHAT_CHANNELS, ("/pi",)),
    CommandSpec("/skills", "/skills", "列出本机可用 skills", _ALL_CHAT_CHANNELS),
    CommandSpec("/remind", "/remind 10m 内容", "定时发送提醒（支持 s/m/h/d）", frozenset({"feishu", "web"}), ("/timer",)),
    CommandSpec("/daily", "/daily HH:MM 提示词", "每日定时简报；支持 list、cancel", _ALL_CHAT_CHANNELS, subcommands=("list", "cancel")),
    CommandSpec("/memory", "/memory", "查看长期记忆；支持 undo", _ALL_CHAT_CHANNELS, subcommands=("status", "undo")),
    CommandSpec("/dream", "/dream", "整理当前对话记忆；支持 all", _ALL_CHAT_CHANNELS, subcommands=("all",)),
)


def command_catalog(channel: str) -> list[dict[str, Any]]:
    return [
        {"command": spec.command, "usage": spec.usage, "description": spec.description,
         "aliases": list(spec.aliases), "subcommands": list(spec.subcommands)}
        for spec in COMMAND_SPECS if channel in spec.channels
    ]


def build_help_text(*, include_remind: bool = True, channel: str = "feishu") -> str:
    selected = command_catalog(channel)
    if not include_remind:
        selected = [item for item in selected if item["command"] != "/remind"]
    lines = ["可用命令:"]
    for item in selected:
        names = " ".join([item["command"], *item["aliases"]])
        usage = item["usage"] if not item["aliases"] else item["usage"].replace(item["command"], names, 1)
        lines.append(f"{usage} - {item['description']}")
    if not include_remind:
        lines.append("/remind - 微信渠道暂不支持")
    return "\n".join(lines)


HELP_TEXT = build_help_text()
REMOVED_BACKEND_COMMANDS = {"/codex", "/claude", "/qodercli", "/opencode"}


@dataclass(slots=True)
class CommandResult:
    handled: bool
    reply_text: str


@dataclass(slots=True)
class ReminderCommand:
    delay_seconds: float
    text: str


@dataclass(slots=True)
class DailyCommand:
    action: str  # "create" | "list" | "cancel" | "invalid"
    hour: int = 0
    minute: int = 0
    prompt: str = ""
    task_id_prefix: str = ""
    error: str = ""


def parse_daily_command(raw_text: str) -> DailyCommand | None:
    text = raw_text.strip()
    if not text.lower().startswith("/daily"):
        return None

    rest = text[len("/daily"):].strip()
    if not rest or rest.lower() in {"list", "ls"}:
        return DailyCommand(action="list")

    cancel_match = re.match(r"^cancel\s+(\S+)$", rest, re.IGNORECASE)
    if cancel_match is not None:
        return DailyCommand(action="cancel", task_id_prefix=cancel_match.group(1))

    create_match = re.match(r"^(\d{1,2}):(\d{2})\s+(.+)$", rest, re.DOTALL)
    if create_match is not None:
        hour = int(create_match.group(1))
        minute = int(create_match.group(2))
        prompt = create_match.group(3).strip()
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return DailyCommand(action="invalid", error="时间格式错误，应为 HH:MM（00:00-23:59）。")
        if not prompt:
            return DailyCommand(action="invalid", error="缺少简报提示词。")
        return DailyCommand(action="create", hour=hour, minute=minute, prompt=prompt)

    return DailyCommand(
        action="invalid",
        error="用法: /daily HH:MM 提示词 | /daily list | /daily cancel <id前缀>",
    )


async def execute_daily_command(daily: DailyCommand, scheduler: Any, channel: str, target_id: str) -> str:
    if daily.action == "invalid":
        return daily.error

    if daily.action == "create":
        task = await scheduler.schedule(
            channel=channel,
            target_id=target_id,
            prompt=daily.prompt,
            hour=daily.hour,
            minute=daily.minute,
        )
        return f"已创建每日简报任务 {task.task_id[:8]}，每天 {daily.hour:02d}:{daily.minute:02d} 执行。"

    if daily.action == "cancel":
        task = await scheduler.cancel(daily.task_id_prefix)
        if task is None:
            return f"未找到唯一匹配的任务: {daily.task_id_prefix}（用 /daily list 查看）。"
        return f"已取消每日简报任务 {task.task_id[:8]}。"

    tasks = scheduler.list_tasks()
    if not tasks:
        return "当前没有每日简报任务。用 /daily HH:MM 提示词 创建。"
    lines = ["每日简报任务:"]
    for task in tasks:
        summary = task.prompt if len(task.prompt) <= 30 else f"{task.prompt[:30]}…"
        paused = "（已停用）" if not getattr(task, "enabled", True) else ""
        lines.append(f"- {task.task_id[:8]} {task.hour:02d}:{task.minute:02d} [{task.channel}]{paused} {summary}")
    return "\n".join(lines)


_MEMORY_USAGE = ("用法: /memory 查看记忆状态 | /memory undo 撤销最近一次 Dream 整理 | "
                 "/dream 整理当前对话 | /dream all 整理全部对话")


def parse_memory_command(raw_text: str) -> str | None:
    """Return "status" | "undo" | "dream" | "dream_all" | "invalid", or None when not a memory command."""
    words = raw_text.strip().lower().split()
    if not words or words[0] not in {"/memory", "/dream"}:
        return None
    if words == ["/dream"]:
        return "dream"
    if words == ["/dream", "all"]:
        return "dream_all"
    if words in (["/memory"], ["/memory", "status"]):
        return "status"
    if words == ["/memory", "undo"]:
        return "undo"
    return "invalid"


async def execute_memory_command(action: str, runner: Any, channel: str, target_id: str,
                                 session_key: str | None = None) -> str:
    if action == "invalid":
        return _MEMORY_USAGE
    if runner is None:
        return "记忆整理组件未启用。"
    if action == "dream":
        return runner.trigger(channel=channel, target_id=target_id, session_key=session_key)
    if action == "dream_all":
        return runner.trigger(channel=channel, target_id=target_id)
    if action == "undo":
        return await runner.undo()
    return await asyncio.to_thread(runner.status)


def process_command(
    raw_text: str,
    session_manager: SessionManager,
    session_key: str,
    agent_client: AgentClient | None = None,
) -> CommandResult | None:
    text = raw_text.strip().lower()
    command = text.split(maxsplit=1)[0] if text else ""

    if text == "/help":
        return CommandResult(handled=True, reply_text=HELP_TEXT)

    if text in {"/new", "/reset"}:
        session_manager.reset_session(session_key)
        if agent_client is not None:
            agent_client.reset_session(session_key)
        reply = "已创建新会话，不再继承历史，待处理附件已清空。" if text == "/new" else "已清空当前会话上下文及待处理附件。"
        return CommandResult(handled=True, reply_text=reply)

    if command in {"/compact", "/compress"}:
        return CommandResult(
            handled=True,
            reply_text="上下文由 pi 自动管理，桥接层不支持手工压缩。",
        )

    if command in {"/backend", "/pi"}:
        return CommandResult(handled=True, reply_text="当前唯一后端为 pi，不支持切换后端。")

    if command in REMOVED_BACKEND_COMMANDS:
        return CommandResult(
            handled=True,
            reply_text=f"{command} 后端已移除，不再支持。当前唯一后端为 pi。",
        )

    return None


def parse_reminder_command(raw_text: str) -> ReminderCommand | None:
    text = raw_text.strip()
    match = re.match(r"^/(?:remind|timer)\s+(\d+(?:\.\d+)?)([smhdSMHD])\s+(.+)$", text, re.DOTALL)
    if match is None:
        return None

    amount = float(match.group(1))
    unit = match.group(2).lower()
    reminder_text = match.group(3).strip()
    if amount <= 0 or not reminder_text:
        return None

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }
    return ReminderCommand(delay_seconds=amount * multipliers[unit], text=reminder_text)
