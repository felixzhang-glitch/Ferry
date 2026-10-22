"""Console configuration: a fixed menu of live-applicable settings and actions.

The console may only pick from the options defined here; any other value is
rejected, which is what keeps it "select only". Choices persist to
runtime/server/console-settings.json, applied after conf/.env at startup and
winning over it. Nothing here returns secrets, paths or memory text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app import mailer, memory
from app.config import Settings

logger = logging.getLogger(__name__)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MODELS_PATH = os.path.join(_ROOT, "conf", "pi", "models.json")
SUMMARY_CHARS = 20
TIME_STEP_MINUTES = 5
_WECHAT_USER = re.compile(r"^[^:\s]+@im\.wechat$")
_FEISHU_CHAT = re.compile(r"^oc_[A-Za-z0-9_-]+$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

GROUPS = (
    ("dream", "记忆整理"),
    ("model", "模型"),
    ("memory", "记忆"),
    ("message", "消息"),
    ("system", "系统"),
)
THINKING_LEVELS = (("off", "关闭"), ("minimal", "极少"), ("low", "低"), ("medium", "中"), ("high", "高"))
LOG_LEVELS = (("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR"))
INJECT_LIMITS = (2000, 4000, 6000, 8000)


class ConsoleError(ValueError):
    """A choice outside the menu, or an action that cannot run right now."""


@dataclass(frozen=True)
class Spec:
    key: str
    group: str
    label: str
    kind: str  # bool | choice | multi | time
    hint: str = ""
    choices: Callable[[Settings], list[dict[str, str]]] | None = None
    allow_immediate: bool = False


# ---------------------------------------------------------------- choices

def _option(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def model_choices(settings: Settings) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    try:
        with open(MODELS_PATH, encoding="utf-8") as fh:
            providers = json.load(fh).get("providers") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        providers = {}
    for provider, block in providers.items():
        for model in (block or {}).get("models") or []:
            model_id, name = str(model.get("id", "")), str(model.get("name", ""))
            if model_id and "勿用" not in name:
                options.append(_option(f"{provider}/{model_id}", name or model_id))
    return options


def _with_current(options: list[dict[str, str]], current: str, label: str = "当前值") -> list[dict[str, str]]:
    if current and current not in {option["value"] for option in options}:
        options = [*options, _option(current, f"{current}（{label}）")]
    return options


def mask_target(value: str) -> str:
    channel, _, target = value.partition(":")
    if channel == "email":
        local, _, domain = target.partition("@")
        return f"邮件 · {local[:1]}***@{domain}"
    if channel == "wechat":
        user = target.split("@", 1)[0]
        return f"微信 · {user[:4]}…{user[-2:]}" if len(user) > 6 else f"微信 · {user}"
    if channel == "feishu":
        return f"飞书 · {target[:6]}…" if len(target) > 8 else f"飞书 · {target}"
    if channel == "web":
        return f"PWA 会话 · {target[:8]}"
    return value


def _chat_targets(settings: Settings) -> list[str]:
    from app.dream import is_chat_key
    from core.agent.pi_cli import SESSION_HISTORY_FILENAME

    store_path = os.path.abspath(settings.pi_session_store_path)
    keys: list[str] = []
    live_web: set[str] = set()
    try:
        with open(store_path, encoding="utf-8") as fh:
            mapping = json.load(fh)
        if isinstance(mapping, dict):
            keys.extend(str(key) for key in mapping)
            live_web = {key for key in keys if re.fullmatch(r"web:[a-f0-9]{32}", key)}
    except (OSError, json.JSONDecodeError):
        pass
    try:
        with open(os.path.join(os.path.dirname(store_path), SESSION_HISTORY_FILENAME), encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    keys.append(str(record.get("session_key", "")))
    except OSError:
        pass

    targets: list[str] = []
    for key in keys:
        if key in live_web:
            if key not in targets:
                targets.append(key)
            continue
        if not is_chat_key(key):
            continue
        parts = key.split(":")
        if parts[0] == "wechat" and len(parts) == 3 and _WECHAT_USER.match(parts[2]):
            target = f"wechat:{parts[2]}"
        elif len(parts) == 2 and _FEISHU_CHAT.match(parts[1]):
            target = f"feishu:{parts[1]}"
        else:
            continue
        if target not in targets:
            targets.append(target)
    return targets


def target_choices(settings: Settings, extra: list[str] | None = None) -> list[dict[str, str]]:
    values = [f"email:{address}" for address in mailer.recipients(settings)]
    for value in [*_chat_targets(settings), *(extra or [])]:
        if value and value not in values:
            values.append(value)
    return [_option(value, mask_target(value)) for value in values]


def _notify_choices(settings: Settings) -> list[dict[str, str]]:
    current = [item.strip() for item in settings.dream_notify.split(",") if item.strip()]
    return [option for option in target_choices(settings, current) if not option["value"].startswith("web:")]


def _dream_model_choices(settings: Settings) -> list[dict[str, str]]:
    return [_option("", "跟随主模型"), *_with_current(model_choices(settings), settings.dream_model)]


def _pi_model_choices(settings: Settings) -> list[dict[str, str]]:
    return _with_current(model_choices(settings), settings.pi_model)


def _category_choices(settings: Settings) -> list[dict[str, str]]:
    return [_option(name, memory.label(name)) for name in memory.categories(settings)]


def _inject_choices(settings: Settings) -> list[dict[str, str]]:
    options = [_option(str(value), f"{value} 字") for value in INJECT_LIMITS]
    return _with_current(options, str(settings.memory_max_inject_chars))


SPECS: tuple[Spec, ...] = (
    Spec("dream_enabled", "dream", "Dream 定时整理", "bool", "关闭后不再定时整理，手动 /dream 也不可用"),
    Spec("dream_time", "dream", "整理时间", "time", "每天在这个时间读取聊天增量并整理记忆"),
    Spec("dream_report_time", "dream", "报告推送时间", "time", "整理结果在这个时间推送；也可整理完立即推送",
         allow_immediate=True),
    Spec("dream_notify", "dream", "报告推送目标", "multi", "可多选；无变更的整理不推送", _notify_choices),
    Spec("dream_model", "dream", "整理所用模型", "choice", "", _dream_model_choices),
    Spec("pi_model", "model", "对话模型", "choice", "PWA、飞书、微信统一使用，下一轮生效", _pi_model_choices),
    Spec("pi_thinking", "model", "思考强度", "choice", "PWA、飞书、微信统一使用，下一轮生效",
         lambda _s: [_option(value, label) for value, label in THINKING_LEVELS]),
    Spec("memory_enabled", "memory", "长期记忆", "bool", "关闭后不再注入记忆，Dream 也停止"),
    Spec("memory_always_inject", "memory", "常驻注入类别", "multi", "每轮都放进上下文的类别", _category_choices),
    Spec("memory_max_inject_chars", "memory", "注入字数上限", "choice", "超出按类别截断", _inject_choices),
    Spec("streaming_enabled", "message", "流式回复", "bool", "飞书卡片与微信分段的流式输出"),
    Spec("log_level", "system", "日志级别", "choice", "",
         lambda _s: [_option(value, label) for value, label in LOG_LEVELS]),
)
SPEC_BY_KEY = {spec.key: spec for spec in SPECS}


# ---------------------------------------------------------------- values

def _to_form(spec: Spec, value: Any) -> Any:
    if spec.kind == "bool":
        return bool(value)
    if spec.kind == "multi":
        return [item.strip() for item in str(value or "").split(",") if item.strip()]
    return str(value)


def normalize(spec: Spec, value: Any, settings: Settings) -> Any:
    """Validate a console choice and convert it to the Settings representation."""
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ConsoleError(f"{spec.label}：只能选择开或关")
        return value
    if spec.kind == "time":
        if spec.allow_immediate and value == "immediate":
            return value
        match = _TIME.match(value) if isinstance(value, str) else None
        if match is None or int(match.group(2)) % TIME_STEP_MINUTES:
            raise ConsoleError(f"{spec.label}：请选择列表中的时间")
        return value
    allowed = {option["value"] for option in (spec.choices(settings) if spec.choices else [])}
    if spec.kind == "multi":
        if not isinstance(value, list) or len(value) > 20 or not all(isinstance(item, str) for item in value):
            raise ConsoleError(f"{spec.label}：请从列表中选择")
        picked = list(dict.fromkeys(value))
        if any(item not in allowed for item in picked):
            raise ConsoleError(f"{spec.label}：包含不在列表中的选项")
        return ",".join(picked)
    if not isinstance(value, str) or value not in allowed:
        raise ConsoleError(f"{spec.label}：请从列表中选择")
    if spec.key == "memory_max_inject_chars":
        return int(value)
    return value


def baseline(settings: Settings) -> dict[str, Any]:
    return {spec.key: getattr(settings, spec.key) for spec in SPECS}


def overrides_path(settings: Settings) -> str:
    return os.path.join(os.path.dirname(memory.index_path(settings)), "console-settings.json")


def load_overrides(settings: Settings, path: str | None = None) -> dict[str, Any]:
    """Apply persisted console choices over conf/.env; invalid entries are skipped."""
    path = path or overrides_path(settings)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("console overrides unreadable, ignoring", extra={"event": "console.load"})
        return {}
    applied: dict[str, Any] = {}
    for key, value in (data.items() if isinstance(data, dict) else []):
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            continue
        try:
            setattr(settings, key, normalize(spec, value, settings))
            applied[key] = value
        except ConsoleError:
            logger.warning("console override rejected", extra={"event": "console.load", "key": key})
    return applied


def _summary(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= SUMMARY_CHARS else f"{flat[:SUMMARY_CHARS]}…"


# ---------------------------------------------------------------- console

class Console:
    def __init__(
        self,
        settings: Settings,
        *,
        defaults: dict[str, Any],
        overrides: dict[str, Any],
        dream_runner: Any = None,
        agent_client: Any = None,
        daily_scheduler: Any = None,
        reminder_scheduler: Any = None,
        path: str | None = None,
    ) -> None:
        self._settings = settings
        self._defaults = dict(defaults)
        self._overrides = dict(overrides)
        self._dream = dream_runner
        self._agent = agent_client
        self._daily = daily_scheduler
        self._reminders = reminder_scheduler
        self._path = path or overrides_path(settings)
        self._lock = asyncio.Lock()

    # -- state

    def state(self) -> dict[str, Any]:
        settings = self._settings
        items = []
        for spec in SPECS:
            entry: dict[str, Any] = {
                "key": spec.key,
                "group": spec.group,
                "label": spec.label,
                "hint": spec.hint,
                "kind": spec.kind,
                "value": _to_form(spec, getattr(settings, spec.key)),
                "default": _to_form(spec, self._defaults.get(spec.key)),
                "overridden": spec.key in self._overrides,
            }
            if spec.choices:
                entry["choices"] = spec.choices(settings)
            if spec.kind == "time":
                entry["step"] = TIME_STEP_MINUTES
                entry["allow_immediate"] = spec.allow_immediate
            items.append(entry)

        targets = target_choices(settings, [f"{task.channel}:{task.target_id}" for task in self._daily_tasks()])
        dream = self._dream.snapshot() if self._dream is not None else None
        if dream is not None:
            dream["targets"] = [_option(value, mask_target(value)) for value in dream["targets"]]

        index = memory.build_index(settings)
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "groups": [{"id": group, "label": label} for group, label in GROUPS],
            "settings": items,
            "dream": dream,
            "memory": [
                {"category": name, "label": entry["label"], "active": entry["counts"]["active"],
                 "archived": entry["counts"]["archived"], "resident": entry["resident"],
                 "last_date": entry["last_date"]}
                for name, entry in index["categories"].items()
            ],
            "targets": targets,
            "daily": [
                {
                    "id": task.task_id,
                    "short_id": task.task_id[:8],
                    "hour": task.hour,
                    "minute": task.minute,
                    "enabled": bool(getattr(task, "enabled", True)),
                    "target": f"{task.channel}:{task.target_id}",
                    "target_label": mask_target(f"{task.channel}:{task.target_id}"),
                    "summary": _summary(task.prompt),
                    "last_run_date": task.last_run_date,
                }
                for task in self._daily_tasks()
            ],
            "reminders": [
                {"id": item.reminder_id, "due_at": item.due_at, "summary": _summary(item.text)}
                for item in (self._reminders.list_reminders() if self._reminders is not None else [])
            ],
        }

    def _daily_tasks(self) -> list[Any]:
        return self._daily.list_tasks() if self._daily is not None else []

    # -- settings

    async def set(self, key: str, value: Any) -> str:
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise ConsoleError("未知配置项")
        async with self._lock:
            normalized = normalize(spec, value, self._settings)
            setattr(self._settings, key, normalized)
            self._overrides[key] = _to_form(spec, normalized)
            await asyncio.to_thread(self._persist)
            self._after_change(key)
        return f"已更新：{spec.label}"

    async def reset(self, key: str) -> str:
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise ConsoleError("未知配置项")
        async with self._lock:
            setattr(self._settings, key, self._defaults.get(key))
            self._overrides.pop(key, None)
            await asyncio.to_thread(self._persist)
            self._after_change(key)
        return f"已恢复默认：{spec.label}"

    def _after_change(self, key: str) -> None:
        if key.startswith("dream_") or key == "memory_enabled":
            if self._dream is not None:
                self._dream.reschedule()
        if key in ("pi_model", "pi_thinking") and self._agent is not None:
            setter = getattr(self._agent, "set_runtime_options", None)
            if setter is not None:
                setter(model=self._settings.pi_model, thinking=self._settings.pi_thinking)
        if key == "log_level":
            logging.getLogger().setLevel(str(self._settings.log_level).upper())
        logger.info("console setting changed", extra={"event": "console.set", "key": key})

    def _persist(self) -> None:
        tmp_path = f"{self._path}.tmp"
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self._overrides, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._path)

    # -- actions

    def dream_run(self) -> str:
        if self._dream is None:
            raise ConsoleError("Dream 未启用")
        ok, message = self._dream.trigger_console()
        if not ok:
            raise ConsoleError(message)
        return message

    async def dream_undo(self) -> str:
        if self._dream is None:
            raise ConsoleError("Dream 未启用")
        return await self._dream.undo()

    async def daily_update(self, task_id: str, *, enabled: bool | None = None, hour: int | None = None,
                           minute: int | None = None, target: str | None = None) -> str:
        if self._daily is None:
            raise ConsoleError("每日任务组件未启用")
        if minute is not None and minute % TIME_STEP_MINUTES:
            raise ConsoleError("请选择列表中的时间")
        channel = target_id = None
        if target is not None:
            task = self._daily.get(task_id)
            current = f"{task.channel}:{task.target_id}" if task is not None else ""
            allowed = {option["value"] for option in target_choices(self._settings, [current])}
            if target not in allowed:
                raise ConsoleError("推送目标不在列表中")
            channel, _, target_id = target.partition(":")
        try:
            task = await self._daily.update(task_id, enabled=enabled, hour=hour, minute=minute,
                                            channel=channel, target_id=target_id)
        except ValueError as exc:
            raise ConsoleError(str(exc)) from exc
        if task is None:
            raise ConsoleError("任务不存在")
        return f"已更新任务 {task.task_id[:8]}"

    async def daily_delete(self, task_id: str) -> str:
        if self._daily is None or self._daily.get(task_id) is None:
            raise ConsoleError("任务不存在")
        task = await self._daily.cancel(task_id)
        if task is None:
            raise ConsoleError("任务不存在")
        return f"已删除任务 {task.task_id[:8]}"

    async def reminder_cancel(self, reminder_id: str) -> str:
        if self._reminders is None or not await self._reminders.cancel(reminder_id):
            raise ConsoleError("提醒不存在")
        return "已取消提醒"
