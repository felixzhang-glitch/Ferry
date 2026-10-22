"""Dream: offline consolidation of long-term memory.

Nightly at DREAM_TIME (or on /dream), read chat transcripts since the last
run, ask pi -- no tools, no session -- for JSON memory ops, then validate and
apply them here as one git commit that `/memory undo` can revert. The model
only proposes; every write is checked and performed by this module.
"""

from __future__ import annotations

import asyncio
import datetime
import glob
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from app import memory, memory_format
from app.clock import TIMEZONE
from app.config import Settings
from app.mailer import valid_address
from app.memory_format import KEY_PATTERN, DATE_PATTERN, MemoryNote, Observation
from core.agent.pi_cli import SESSION_HISTORY_FILENAME
from core.session.daily_scheduler import CATCHUP_WINDOW_SECONDS
from observability.telemetry import emit, observe_run, set_status

logger = logging.getLogger(__name__)

PushCallback = Callable[[str, str, str, str], Awaitable[None]]

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
RULES_PATH = os.path.join(_ROOT, "rules", "dream.md")
ADMIN_RULES_PATH = os.path.join(_ROOT, "rules", "admin.md")

FIRST_RUN_LOOKBACK_DAYS = 7
MAX_OPS = 30
RECENT_ARCHIVE_DAYS = 90
USER_MESSAGE_CHARS = 1000
ASSISTANT_MESSAGE_CHARS = 200
CONTENT_MAX_CHARS = 200
MIN_EVIDENCE_CHARS = 4
DIGEST_ITEMS_PER_GROUP = 8

_SKILL_PREAMBLE = "本机可用 skills 如下"
_USER_PREFIX = "用户: "
_FEISHU_CHAT_KEY = re.compile(r"^[^:]+:oc_[^:]+$")
_SECRET = re.compile(
    r"(?i)(password|passwd|密码|口令|验证码|token|secret|api[_-]?key|私钥|"
    r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|(?<!\d)\d{16,19}(?!\d)|(?<!\d)\d{17}[\dX](?!\d))"
)
_OP_LABELS = (("add", "新增"), ("update", "更新"), ("merge", "合并"), ("archive", "归档"))
_STATUS_LABELS = {"applied": "已生效", "empty": "无变更", "failed": "失败", "conflict": "有冲突"}


@dataclass
class Turn:
    at: datetime.datetime
    role: str
    text: str


@dataclass
class Change:
    op: str
    category: str
    key: str
    after: str = ""
    before: list[str] = field(default_factory=list)
    evidence: str = ""
    reason: str = ""

    def describe(self) -> str:
        where = f"{self.category}/{self.key}"
        if self.op == "add":
            return f"{where}：{self.after}"
        if self.op == "update":
            return f"{where}：{self.before[0] if self.before else ''} → {self.after}"
        if self.op == "merge":
            return f"{where}：{len(self.before)} 条 → {self.after}"
        suffix = f"（{self.reason}）" if self.reason else ""
        return f"{where}：{self.before[0] if self.before else ''}{suffix}"


@dataclass
class DreamResult:
    status: str
    message: str = ""
    changes: list[Change] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    commit: str | None = None


# ---------------------------------------------------------------- transcripts

def is_chat_key(session_key: str) -> bool:
    """Real conversations only: WeChat keys and Feishu `user:oc_chat` keys."""
    return session_key.startswith(("wechat:", "web:")) or bool(_FEISHU_CHAT_KEY.match(session_key))


def _sessions_dir(settings: Settings) -> str:
    agent_dir = settings.pi_agent_dir.strip()
    base = os.path.expanduser(agent_dir) if agent_dir else os.path.join(os.path.expanduser("~"), ".pi", "agent")
    return os.path.join(base, "sessions")


def chat_sessions(settings: Settings) -> dict[str, str]:
    """Session id -> chat key, from the append-only history plus the live mapping."""
    store_path = os.path.abspath(settings.pi_session_store_path)
    sessions: dict[str, str] = {}
    retired: set[str] = set()
    history_path = os.path.join(os.path.dirname(store_path), SESSION_HISTORY_FILENAME)
    try:
        with open(history_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                sid, key = record.get("session_id"), str(record.get("session_key", ""))
                if record.get("event") == "retired":
                    retired.add(key)
                    sessions = {known_sid: known_key for known_sid, known_key in sessions.items() if known_key != key}
                elif isinstance(sid, str) and sid and is_chat_key(key):
                    retired.discard(key)
                    sessions[sid] = key
    except OSError:
        pass
    try:
        with open(store_path, encoding="utf-8") as fh:
            mapping = json.load(fh)
        if isinstance(mapping, dict):
            sessions.update((sid, str(key)) for key, sid in mapping.items()
                            if isinstance(sid, str) and is_chat_key(str(key)) and str(key) not in retired)
    except (OSError, json.JSONDecodeError):
        pass
    return sessions


def session_files(settings: Settings, session_ids: set[str]) -> list[str]:
    root = _sessions_dir(settings)
    files: list[str] = []
    for sid in sorted(session_ids):
        if re.fullmatch(r"[A-Za-z0-9-]+", sid):
            files.extend(glob.glob(os.path.join(root, "*", f"*_{sid}.jsonl")))
    return sorted(set(files))


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        item["text"] for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    )


def _clean_user_text(text: str) -> str:
    # The first turn of a pi session carries the skills summary before the user text.
    if text.startswith(_SKILL_PREAMBLE):
        _, sep, rest = text.partition(f"\n{_USER_PREFIX}")
        text = rest if sep else ""
    elif text.startswith(_USER_PREFIX):
        text = text[len(_USER_PREFIX):]
    return text.strip()


def collect_turns(paths: list[str], since: datetime.datetime) -> list[Turn]:
    """User and assistant text after `since`; tool calls, thinking and system snapshots are skipped."""
    turns: list[Turn] = []
    for path in paths:
        try:
            if os.path.getmtime(path) < since.timestamp():
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"message"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict) or entry.get("type") != "message":
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                        continue
                    at = _parse_timestamp(entry.get("timestamp"))
                    if at is None or at <= since:
                        continue
                    text = _message_text(message)
                    if message["role"] == "user":
                        text = _clean_user_text(text)
                    text = " ".join(text.split())
                    if text:
                        turns.append(Turn(at, message["role"], text))
        except OSError:
            logger.warning("failed to read pi transcript", extra={"event": "dream.transcript", "path": path})
    turns.sort(key=lambda turn: turn.at)
    return turns


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def window_turns(turns: list[Turn], limit: int) -> list[tuple[Turn, str]]:
    """Newest turns that fit the character budget, returned oldest first with their rendered line."""
    kept: list[tuple[Turn, str]] = []
    used = 0
    for turn in reversed(turns):
        cap = USER_MESSAGE_CHARS if turn.role == "user" else ASSISTANT_MESSAGE_CHARS
        stamp = turn.at.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")
        speaker = "用户" if turn.role == "user" else "助手"
        line = f"[{stamp} {speaker}] {_clip(turn.text, cap)}"
        if kept and used + len(line) + 1 > limit:
            break
        kept.append((turn, line))
        used += len(line) + 1
    kept.reverse()
    return kept


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


# ---------------------------------------------------------------- planning

@dataclass
class _Snapshot:
    notes: dict[str, MemoryNote]
    shas: dict[str, str]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def take_snapshot(settings: Settings) -> _Snapshot:
    notes: dict[str, MemoryNote] = {}
    shas: dict[str, str] = {}
    for name in memory.categories(settings):
        text = memory.read_text(name, settings)
        notes[name] = memory_format.parse_note(text)
        shas[name] = _sha(text)
    return _Snapshot(notes, shas)


class _Plan:
    """Validated ops as per-category line patches, plus a human-readable change list."""

    def __init__(self, snapshot: _Snapshot, today: str) -> None:
        self.snapshot = snapshot
        self.today = today
        self.source = f"dream {today}"
        self.replace: dict[str, dict[int, str]] = {name: {} for name in snapshot.notes}
        self.archive: dict[str, dict[int, str]] = {name: {} for name in snapshot.notes}
        self.append: dict[str, list[str]] = {name: [] for name in snapshot.notes}
        self.added: dict[str, set[str]] = {name: set() for name in snapshot.notes}
        self.touched: set[tuple[str, int]] = set()
        self.changes: list[Change] = []
        self.rejected: list[dict[str, Any]] = []
        self.evidence: list[tuple[str, str]] = []

    # -- deterministic maintenance

    def archive_stale_recent(self) -> None:
        note = self.snapshot.notes.get("recent")
        if note is None:
            return
        cutoff = (datetime.date.fromisoformat(self.today) - datetime.timedelta(days=RECENT_ARCHIVE_DAYS)).isoformat()
        for obs in note.active:
            if obs.date and obs.date < cutoff:
                self._archive("recent", obs, reason=f"超过 {RECENT_ARCHIVE_DAYS} 天")

    def stale_lines(self) -> set[tuple[str, int]]:
        return set(self.touched)

    # -- model ops

    def set_evidence(self, windowed: list[tuple[Turn, str]]) -> None:
        self.evidence = [
            (turn.at.astimezone(TIMEZONE).strftime("%Y-%m-%d"), _normalize(_clip(turn.text, USER_MESSAGE_CHARS)))
            for turn, _ in windowed if turn.role == "user"
        ]

    def apply_ops(self, ops: list[Any]) -> None:
        for index, op in enumerate(ops):
            if index >= MAX_OPS:
                self._reject(op, f"超过单次上限 {MAX_OPS}")
                continue
            if not isinstance(op, dict):
                self._reject(op, "不是对象")
                continue
            handler = {"add": self._op_add, "update": self._op_update,
                       "merge": self._op_merge, "archive": self._op_archive}.get(op.get("op"))
            if handler is None:
                self._reject(op, "未知 op")
                continue
            reason = handler(op)
            if reason:
                self._reject(op, reason)

    def _reject(self, op: Any, reason: str) -> None:
        self.rejected.append({"op": op, "reason": reason})

    def _find_evidence(self, raw: Any) -> str | None:
        """Date of the user message quoting `raw` verbatim (modulo spacing/punctuation)."""
        if not isinstance(raw, str):
            return None
        needle = _normalize(raw)
        if len(needle) < MIN_EVIDENCE_CHARS:
            return None
        for date, haystack in self.evidence:
            if needle in haystack:
                return date
        return None

    def _date(self, op: dict[str, Any], fallback: str) -> str:
        value = op.get("date")
        if isinstance(value, str) and DATE_PATTERN.match(value) and value <= self.today:
            try:
                datetime.date.fromisoformat(value)
                return value
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _content(op: dict[str, Any]) -> tuple[str, str]:
        raw = op.get("content")
        if not isinstance(raw, str):
            return "", "缺少 content"
        text = memory_format.sanitize_content(raw)
        if not text:
            return "", "content 为空"
        if len(text) > CONTENT_MAX_CHARS:
            return "", "content 过长"
        if "[[" in text or "]]" in text:
            return "", "content 含 wikilink"
        if _SECRET.search(text):
            return "", "疑似敏感信息"
        return text, ""

    def _resolve(self, ref: Any) -> tuple[str, Observation] | None:
        if not isinstance(ref, str) or ":" not in ref:
            return None
        category, _, ordinal = ref.partition(":")
        note = self.snapshot.notes.get(category)
        if note is None or not ordinal.isdigit():
            return None
        active = note.active
        position = int(ordinal)
        if not 1 <= position <= len(active):
            return None
        return category, active[position - 1]

    def _key(self, op: dict[str, Any], fallback: str) -> str:
        value = op.get("key")
        return value if isinstance(value, str) and KEY_PATTERN.match(value) else fallback

    def _line(self, key: str, content: str, date: str) -> str:
        return memory_format.format_observation(key, content, date=date, meta={"source": self.source})

    def _archive(self, category: str, obs: Observation, *, reason: str = "", evidence: str = "", merged: bool = False) -> None:
        updates = {"archived": self.today}
        if merged:
            updates["reason"] = "merged"
        self.archive[category][obs.line] = memory_format.observation_line(obs, **updates)
        self.touched.add((category, obs.line))
        if not merged:
            self.changes.append(Change("archive", category, obs.key, before=[obs.content],
                                       evidence=evidence, reason=reason))

    def _op_add(self, op: dict[str, Any]) -> str:
        category = op.get("category")
        if category not in self.snapshot.notes:
            return "未知类别"
        key = op.get("key")
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            return "key 不合法"
        content, error = self._content(op)
        if error:
            return error
        evidence_date = self._find_evidence(op.get("evidence"))
        if evidence_date is None:
            return "evidence 不在用户消息中"
        existing = {_normalize(obs.content) for obs in self.snapshot.notes[category].active}
        existing.update(self.added[category])
        if _normalize(content) in existing:
            return "重复条目"
        self.added[category].add(_normalize(content))
        self.append[category].append(self._line(key, content, self._date(op, evidence_date)))
        self.changes.append(Change("add", category, key, after=content, evidence=str(op.get("evidence"))))
        return ""

    def _op_update(self, op: dict[str, Any]) -> str:
        target = self._resolve(op.get("id"))
        if target is None:
            return "id 不存在"
        category, obs = target
        if (category, obs.line) in self.touched:
            return "条目已被本次其他 op 处理"
        content, error = self._content(op)
        if error:
            return error
        evidence_date = self._find_evidence(op.get("evidence"))
        if evidence_date is None:
            return "evidence 不在用户消息中"
        key = self._key(op, obs.key)
        self.replace[category][obs.line] = self._line(key, content, self._date(op, evidence_date))
        self.touched.add((category, obs.line))
        self.changes.append(Change("update", category, key, after=content, before=[obs.content],
                                   evidence=str(op.get("evidence"))))
        return ""

    def _op_merge(self, op: dict[str, Any]) -> str:
        refs = op.get("ids")
        if not isinstance(refs, list) or len(refs) < 2:
            return "merge 需要至少两个 id"
        targets = [self._resolve(ref) for ref in refs]
        if any(target is None for target in targets):
            return "id 不存在"
        categories = {category for category, _ in targets}
        if len(categories) != 1:
            return "merge 只能在同一类别内"
        category = categories.pop()
        observations = sorted({obs.line: obs for _, obs in targets}.values(), key=lambda obs: obs.line)
        if len(observations) < 2:
            return "merge 需要至少两个不同条目"
        if any((category, obs.line) in self.touched for obs in observations):
            return "条目已被本次其他 op 处理"
        content, error = self._content(op)
        if error:
            return error
        first = observations[0]
        dates = [obs.date for obs in observations if obs.date]
        key = self._key(op, first.key)
        self.replace[category][first.line] = self._line(key, content, self._date(op, max(dates) if dates else ""))
        for obs in observations:
            self._archive(category, obs, merged=True)
        self.changes.append(Change("merge", category, key, after=content,
                                   before=[obs.content for obs in observations]))
        return ""

    def _op_archive(self, op: dict[str, Any]) -> str:
        target = self._resolve(op.get("id"))
        if target is None:
            return "id 不存在"
        category, obs = target
        if (category, obs.line) in self.touched:
            return "条目已被本次其他 op 处理"
        evidence = op.get("evidence")
        if obs.meta.get("source") == "user" and self._find_evidence(evidence) is None:
            return "归档用户条目需要可校验的 evidence"
        reason = op.get("reason") if isinstance(op.get("reason"), str) else ""
        self._archive(category, obs, reason=memory_format.sanitize_content(reason)[:40],
                      evidence=evidence if isinstance(evidence, str) else "")
        return ""

    def categories_changed(self) -> list[str]:
        return [name for name in self.snapshot.notes
                if self.replace[name] or self.archive[name] or self.append[name]]


def parse_ops(text: str) -> list[Any]:
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fence:
        body = fence.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in model output")
    data = json.loads(body[start:end + 1])
    ops = data.get("ops") if isinstance(data, dict) else None
    if not isinstance(ops, list):
        raise ValueError("model output has no ops list")
    return ops


def render_memory_input(settings: Settings, snapshot: _Snapshot, skip: set[tuple[str, int]]) -> str:
    lines = ["# 当前记忆", "", "id 用于 update / merge / archive；同一 key 表示同一主题。", ""]
    for name, note in snapshot.notes.items():
        nature = note.frontmatter.get("nature") or memory.nature(name)
        description = note.frontmatter.get("description") or memory.hint(name)
        lines.append(f"## {name} {memory.label(name)}（nature: {nature}）")
        lines.append(f"说明：{description}")
        shown = 0
        for ordinal, obs in enumerate(note.active, 1):
            if (name, obs.line) in skip:
                continue
            date = f" @{obs.date}" if obs.date else ""
            source = f" (source: {obs.meta['source']})" if obs.meta.get("source") else ""
            lines.append(f"- {name}:{ordinal} [{obs.key}]{date} {obs.content}{source}")
            shown += 1
        if not shown:
            lines.append("（暂无条目）")
        lines.append("")
    return "\n".join(lines)


def render_digest(today: str, changes: list[Change], conflicts: list[str], undo_available: bool) -> str:
    lines = [f"Dream 记忆整理 {today}"]
    for op, title in _OP_LABELS:
        items = [change for change in changes if change.op == op]
        if not items:
            continue
        lines.append(f"{title} {len(items)}")
        lines.extend(f"- {change.describe()}" for change in items[:DIGEST_ITEMS_PER_GROUP])
        if len(items) > DIGEST_ITEMS_PER_GROUP:
            lines.append(f"- …另有 {len(items) - DIGEST_ITEMS_PER_GROUP} 条")
    if conflicts:
        lines.append(f"跳过 {'、'.join(conflicts)}：整理期间被对话修改，下次重试")
    if undo_available:
        lines.append("回复 /memory undo 撤销本次整理")
    return "\n".join(lines)


def _counts(changes: list[Change]) -> dict[str, int]:
    return {op: sum(1 for change in changes if change.op == op) for op, _ in _OP_LABELS}


def _counts_text(counts: dict[str, int]) -> str:
    return " ".join(f"{title}{counts.get(op, 0)}" for op, title in _OP_LABELS if counts.get(op))


def parse_target(value: str) -> tuple[str, str] | None:
    channel, sep, target = value.strip().partition(":")
    target = target.strip()
    if not sep or not target:
        return None
    if channel in ("wechat", "feishu"):
        return channel, target
    if channel == "email" and valid_address(target):
        return channel, target
    return None


def parse_targets(value: str) -> list[tuple[str, str]]:
    """Comma-separated push targets; malformed entries are skipped (warned once per value)."""
    targets: list[tuple[str, str]] = []
    bad = False
    for item in (value or "").split(","):
        if not item.strip():
            continue
        target = parse_target(item)
        if target is None:
            bad = True
        elif target not in targets:
            targets.append(target)
    if bad:
        _warn_once("invalid DREAM_NOTIFY entries skipped", value)
    return targets


_WARNED: set[tuple[str, str]] = set()


def _warn_once(message: str, value: str) -> None:
    if (message, value) not in _WARNED:
        _WARNED.add((message, value))
        logger.warning(message, extra={"event": "dream.config"})


def parse_clock(value: str, fallback: tuple[int, int] = (2, 0)) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return int(match.group(1)), int(match.group(2))
    _warn_once(f"invalid clock value, falling back to {fallback[0]:02d}:{fallback[1]:02d}", value or "")
    return fallback


REPORT_IMMEDIATE = "immediate"


# ---------------------------------------------------------------- runner

class DreamRunner:
    def __init__(self, settings: Settings, agent_client: Any, push_callback: PushCallback | None = None) -> None:
        self._settings = settings
        self._agent = agent_client
        self._push = push_callback
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None
        self._report_task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        self._run_wake = asyncio.Event()
        self._report_wake = asyncio.Event()
        self._dir = os.path.join(os.path.dirname(memory.index_path(settings)), "dream")
        self._state_path = os.path.join(self._dir, "state.json")
        self._state: dict[str, Any] = self._load_state()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.dream_enabled and self._settings.memory_enabled)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def targets(self) -> list[tuple[str, str]]:
        return parse_targets(self._settings.dream_notify)

    def _run_clock(self) -> tuple[int, int]:
        return parse_clock(self._settings.dream_time, (2, 0))

    def _report_immediate(self) -> bool:
        return self._settings.dream_report_time.strip().lower() == REPORT_IMMEDIATE

    def _report_clock(self) -> tuple[int, int]:
        return parse_clock(self._settings.dream_report_time, (8, 0))

    # -- lifecycle

    async def start(self) -> None:
        # Loops always run; `enabled` is checked when a timer fires, so the
        # console can switch Dream on and off without a restart.
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._supervise(self._run_cycle, "run"))
        if self._report_task is None:
            self._report_task = asyncio.create_task(self._supervise(self._report_cycle, "report"))

    async def close(self) -> None:
        tasks = [task for task in (self._loop_task, self._report_task, self._manual_task)
                 if task is not None and not task.done()]
        self._loop_task = self._report_task = self._manual_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def reschedule(self) -> None:
        """Wake both timers so they recompute against the current settings."""
        self._run_wake.set()
        self._report_wake.set()

    async def _supervise(self, cycle: Callable[[], Awaitable[None]], name: str) -> None:
        while True:
            try:
                await cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dream loop crashed; restarting after backoff",
                                 extra={"event": "dream.loop_error", "loop": name})
                await asyncio.sleep(60.0)

    @staticmethod
    async def _wait(event: asyncio.Event, seconds: float | None) -> bool:
        """True when the timer elapsed, False when woken by reschedule()."""
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return True
        event.clear()
        return False

    async def _run_cycle(self) -> None:
        if self._should_catch_up(self._run_clock(), "last_run_date"):
            await self._scheduled_run()
        while True:
            if not await self._wait(self._run_wake, self._seconds_until(self._run_clock(), "last_run_date")):
                continue
            if self._state.get("last_run_date") == self._today():
                continue
            await self._scheduled_run()

    async def _scheduled_run(self) -> None:
        if self.enabled:
            await self.run("scheduled")

    async def _report_cycle(self) -> None:
        if self._state.get("pending_report") and not self._report_immediate() \
                and self._should_catch_up(self._report_clock(), "last_report_date"):
            await self._send_report(mark_day=True)
        while True:
            if self._report_immediate():
                # Switching to immediate flushes whatever is still queued.
                if self._state.get("pending_report"):
                    await self._send_report(mark_day=False)
                await self._wait(self._report_wake, None)
                continue
            if not await self._wait(self._report_wake, self._seconds_until(self._report_clock(), "last_report_date")):
                continue
            if self._state.get("last_report_date") == self._today():
                continue
            await self._send_report(mark_day=True)

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(TIMEZONE)

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _due_today(self, clock: tuple[int, int]) -> datetime.datetime:
        return self._now().replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)

    def _should_catch_up(self, clock: tuple[int, int], marker: str) -> bool:
        if str(self._state.get(marker, "")) >= self._today():
            return False
        elapsed = (self._now() - self._due_today(clock)).total_seconds()
        return 0 <= elapsed <= CATCHUP_WINDOW_SECONDS

    def _next_due(self, clock: tuple[int, int], marker: str) -> datetime.datetime:
        due = self._due_today(clock)
        if due <= self._now() or self._state.get(marker) == self._today():
            due += datetime.timedelta(days=1)
        return due

    def _seconds_until(self, clock: tuple[int, int], marker: str) -> float:
        return max(1.0, (self._next_due(clock, marker) - self._now()).total_seconds())

    # -- commands

    def trigger(self, channel: str, target_id: str, session_key: str | None = None) -> str:
        """Manual run from chat; `session_key` limits it to that conversation."""
        if not self.enabled:
            return "Dream 未启用（需要 DREAM_ENABLED 与 MEMORY_ENABLED）。"
        if self.running or (self._manual_task is not None and not self._manual_task.done()):
            return "Dream 正在整理中，完成后会推送摘要。"
        self._manual_task = asyncio.create_task(
            self.run("manual", reply_to=(channel, target_id), session_key=session_key))
        if session_key:
            return "开始整理当前对话的记忆，完成后推送摘要。"
        return "开始整理记忆，完成后推送摘要。"

    def trigger_console(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Dream 未启用"
        if self.running or (self._manual_task is not None and not self._manual_task.done()):
            return False, "Dream 正在整理中"
        self._manual_task = asyncio.create_task(self.run("console"))
        return True, "已开始整理，完成后推送到报告目标" if self.targets else "已开始整理"

    def snapshot(self) -> dict[str, Any]:
        """Structured status for the console: times, states and counts, never memory text."""
        last = self._state.get("last_run") if isinstance(self._state.get("last_run"), dict) else None
        undoable = self._state.get("last_commit_run")
        report_immediate = self._report_immediate()
        return {
            "enabled": self.enabled,
            "running": self.running,
            "run_time": "{:02d}:{:02d}".format(*self._run_clock()),
            "report_time": REPORT_IMMEDIATE if report_immediate else "{:02d}:{:02d}".format(*self._report_clock()),
            "next_run": self._next_due(self._run_clock(), "last_run_date").isoformat() if self.enabled else None,
            "next_report": None if report_immediate
            else self._next_due(self._report_clock(), "last_report_date").isoformat(),
            "pending_report": bool(self._state.get("pending_report")),
            "targets": [f"{channel}:{target}" for channel, target in self.targets],
            "watermark": self._state.get("watermark"),
            "last_run": None if last is None else {
                "at": last.get("at"),
                "trigger": last.get("trigger"),
                "status": last.get("status"),
                "counts": last.get("counts") or {},
                "undone": bool(last.get("undone")),
            },
            "undo_available": bool(isinstance(undoable, dict) and undoable.get("commit") and not undoable.get("undone")),
        }

    def status(self) -> str:
        index = memory.build_index(self._settings)
        lines = ["长期记忆："]
        for name, entry in index["categories"].items():
            counts = entry["counts"]
            archived = f"，已归档 {counts['archived']}" if counts["archived"] else ""
            resident = "，常驻" if entry["resident"] else ""
            lines.append(f"- {entry['label']}（{name}）{counts['active']} 条{archived}{resident}")
        last = self._state.get("last_run")
        if isinstance(last, dict):
            counts = _counts_text(last.get("counts") or {}) or "无变更"
            undone = "，已撤销" if last.get("undone") else ""
            label = _STATUS_LABELS.get(str(last.get("status")), str(last.get("status")))
            lines.append(f"最近一次 Dream：{str(last.get('at', ''))[:16].replace('T', ' ')}"
                         f"（{label}，{counts}{undone}）")
        else:
            lines.append("Dream 尚未运行过。")
        if self.enabled:
            run_at = "{:02d}:{:02d}".format(*self._run_clock())
            report_at = "整理完立即" if self._report_immediate() else "{:02d}:{:02d}".format(*self._report_clock())
            lines.append(f"定时整理：每天 {run_at}，报告 {report_at} 推送；/dream 整理当前对话，"
                         f"/dream all 整理全部对话，/memory undo 撤销最近一次")
        else:
            lines.append("Dream 未启用。")
        return "\n".join(lines)

    async def undo(self) -> str:
        if self.running:
            return "Dream 正在整理中，稍后再撤销。"
        async with self._lock:
            last = self._state.get("last_commit_run")
            if not isinstance(last, dict) or not last.get("commit"):
                return "没有可撤销的 Dream 整理。"
            if last.get("undone"):
                return f"最近一次 Dream 整理（{last.get('date')}）已经撤销过了。"
            await asyncio.to_thread(memory.auto_commit, self._settings)
            ok, detail = await asyncio.to_thread(memory.revert, self._settings, str(last["commit"]))
            if not ok:
                return f"撤销失败：整理之后记忆又被修改过，自动撤销会冲突，请手工处理。\n{detail}"
            last["undone"] = True
            last["undo_commit"] = detail
            current = self._state.get("last_run")
            if isinstance(current, dict) and current.get("commit") == last["commit"]:
                current["undone"] = True
            self._save_state()
            await asyncio.to_thread(memory.write_context_file, self._settings)
            counts = _counts_text(last.get("counts") or {})
            return f"已撤销 {last.get('date')} 的 Dream 整理（{counts}），记忆恢复到整理前。"

    # -- run

    async def run(self, trigger: str = "manual", reply_to: tuple[str, str] | None = None,
                  session_key: str | None = None, *, deliver: bool = True) -> DreamResult:
        if self.running:
            return DreamResult("busy", "Dream 正在整理中。")
        async with self._lock:
            result = await self._run(trigger=trigger, session_key=session_key)
            if trigger == "scheduled":
                self._state["last_run_date"] = self._today()
                self._save_state()
        if deliver:
            await self._deliver(result, trigger, reply_to)
        return result

    @observe_run(channel="system", source="scheduled")
    async def _run(self, trigger: str, session_key: str | None = None) -> DreamResult:
        settings = self._settings
        started = self._now()
        today = started.strftime("%Y-%m-%d")
        scope = "当前对话" if session_key else ""
        generation_started = time.monotonic()
        raw_output = ""
        status = "error"
        try:
            await asyncio.to_thread(memory.auto_commit, settings)
            pre_commit = await asyncio.to_thread(memory.head, settings)
            snapshot = await asyncio.to_thread(take_snapshot, settings)
            plan = _Plan(snapshot, today)
            plan.archive_stale_recent()

            since = self._watermark() or started - datetime.timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
            turns = await asyncio.to_thread(self._collect, since, session_key)
            windowed = window_turns(turns, max(2000, settings.dream_max_transcript_chars))
            plan.set_evidence(windowed)

            asked = any(turn.role == "user" for turn, _ in windowed)
            if asked:
                raw_output = await self._ask_model(settings, snapshot, plan, windowed, today)
                plan.apply_ops(parse_ops(raw_output))
            status = "success"
        except Exception as exc:
            set_status("error")
            logger.exception("dream run failed", extra={"event": "dream.run_error"})
            result = DreamResult("failed", f"Dream 记忆整理失败：{str(exc)[:200]}。下次定时或发 /dream 重试。")
            self._record_run(result, trigger, today, started, None, raw_output, session_key)
            return result
        finally:
            emit("task", source="scheduled", operation="dream", stage="task_generation", status=status,
                 count=1, duration_seconds=time.monotonic() - generation_started)

        watermark = max((turn.at for turn in turns), default=started)
        if not plan.changes:
            self._advance(watermark, session_key)
            message = (f"Dream 记忆整理：{scope}没有发现需要记住或整理的内容。" if asked
                       else f"Dream 记忆整理：自上次整理以来{scope}没有新的对话。")
            result = DreamResult("empty", message, rejected=plan.rejected)
            self._record_run(result, trigger, today, started, pre_commit, raw_output, session_key)
            set_status("success")
            return result

        conflicts, commit_sha = await asyncio.to_thread(self._apply, plan, today)
        applied = [change for change in plan.changes if change.category not in conflicts]
        if not conflicts:
            self._advance(watermark, session_key)
        await asyncio.to_thread(memory.write_context_file, settings)
        status_name = "conflict" if conflicts and not applied else "applied"
        message = render_digest(today, applied, conflicts, undo_available=bool(commit_sha))
        if applied and not commit_sha:
            message += "\n（记忆快照仓不可用，本次无法 /memory undo）"
        result = DreamResult(status_name, message, applied, plan.rejected, conflicts, commit_sha)
        self._record_run(result, trigger, today, started, pre_commit, raw_output, session_key)
        set_status("success")
        return result

    def _collect(self, since: datetime.datetime, session_key: str | None) -> list[Turn]:
        """Turns after `since`, raised per conversation by that conversation's own watermark."""
        groups: dict[str, set[str]] = {}
        for sid, key in chat_sessions(self._settings).items():
            if session_key is None or key == session_key:
                groups.setdefault(key, set()).add(sid)
        marks = self._chat_marks()
        turns: list[Turn] = []
        for key, sids in groups.items():
            start = max(since, marks.get(key, since))
            turns.extend(collect_turns(session_files(self._settings, sids), start))
        turns.sort(key=lambda turn: turn.at)
        return turns

    async def _ask_model(self, settings: Settings, snapshot: _Snapshot, plan: _Plan,
                         windowed: list[tuple[Turn, str]], today: str) -> str:
        parts = [
            render_memory_input(settings, snapshot, plan.stale_lines()),
            "# 对话增量",
            "",
            *(line for _, line in windowed),
        ]
        os.makedirs(self._dir, exist_ok=True)
        input_path = os.path.join(self._dir, "input.md")
        await asyncio.to_thread(self._write_text, input_path, "\n".join(parts) + "\n")
        system_prompts = [RULES_PATH]
        if os.path.isfile(ADMIN_RULES_PATH):
            system_prompts.append(ADMIN_RULES_PATH)
        prompt = f"今天是 {today}。按系统提示整理附件中的记忆与对话增量，只输出 JSON。"
        return await self._agent.complete(
            prompt,
            system_prompts=system_prompts,
            attachments=[input_path],
            model=settings.dream_model.strip() or None,
            timeout_seconds=settings.dream_timeout_seconds,
        )

    def _apply(self, plan: _Plan, today: str) -> tuple[list[str], str | None]:
        conflicts: list[str] = []
        wrote = False
        for name in plan.categories_changed():
            current = memory.read_text(name, self._settings)
            if _sha(current) != plan.snapshot.shas[name]:
                conflicts.append(name)
                continue
            text = memory_format.patch_note(plan.snapshot.notes[name], replace=plan.replace[name],
                                            archive=plan.archive[name], append=plan.append[name])
            if memory.write_category(name, text, self._settings):
                wrote = True
            else:
                conflicts.append(name)
        if not wrote:
            return conflicts, None
        counts = _counts([change for change in plan.changes if change.category not in conflicts])
        message = (f"dream: {today} +{counts['add']} ~{counts['update']} "
                   f"merge {counts['merge']} -{counts['archive']}")
        return conflicts, memory.commit(self._settings, message)

    # -- delivery

    async def _deliver(self, result: DreamResult, trigger: str, reply_to: tuple[str, str] | None) -> None:
        if result.status == "busy":
            return
        if trigger == "manual" and reply_to:
            await self._push_to([reply_to], result.message)
            return
        if result.status == "empty":
            return
        if trigger == "scheduled" and not self._report_immediate():
            self._queue_report(result.message)
            return
        await self._push_to(self.targets, result.message)

    def _queue_report(self, message: str) -> None:
        pending = self._state.get("pending_report")
        if isinstance(pending, dict) and pending.get("message"):
            message = f"{pending['message']}\n\n{message}"
        self._state["pending_report"] = {"queued_at": self._now().isoformat(), "message": message}
        self._save_state()

    async def _send_report(self, *, mark_day: bool) -> None:
        pending = self._state.pop("pending_report", None)
        if mark_day:
            self._state["last_report_date"] = self._today()
        self._save_state()
        if isinstance(pending, dict) and pending.get("message"):
            delivered = await self._push_to(self.targets, str(pending["message"]))
            emit("task", source="scheduled", operation="dream", stage="task_delivery",
                 status="success" if delivered else "error", count=delivered)

    async def _push_to(self, targets: list[tuple[str, str]], message: str) -> int:
        if self._push is None:
            return 0
        delivered = 0
        trace_id = f"dream-{self._today()}-{int(time.time())}"
        for channel, target in targets:
            try:
                await self._push(channel, target, message, trace_id)
                delivered += 1
            except Exception:
                logger.exception("dream digest push failed", extra={"event": "dream.push_error", "channel": channel})
        return delivered

    # -- state

    def _watermark(self) -> datetime.datetime | None:
        return _parse_timestamp(self._state.get("watermark"))

    def _chat_marks(self) -> dict[str, datetime.datetime]:
        raw = self._state.get("chat_watermarks")
        marks: dict[str, datetime.datetime] = {}
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            parsed = _parse_timestamp(value)
            if parsed is not None:
                marks[str(key)] = parsed
        return marks

    def _advance(self, watermark: datetime.datetime, session_key: str | None = None) -> None:
        marks = self._chat_marks()
        if session_key is None:
            current = self._watermark()
            if current is None or watermark > current:
                self._state["watermark"] = watermark.isoformat()
            floor = self._watermark()
            # A conversation mark at or below the global one no longer narrows anything.
            marks = {key: mark for key, mark in marks.items() if floor is None or mark > floor}
        elif session_key not in marks or watermark > marks[session_key]:
            marks[session_key] = watermark
        if marks:
            self._state["chat_watermarks"] = {key: mark.isoformat() for key, mark in marks.items()}
        else:
            self._state.pop("chat_watermarks", None)

    def _record_run(self, result: DreamResult, trigger: str, today: str, started: datetime.datetime,
                    pre_commit: str | None, raw_output: str, session_key: str | None = None) -> None:
        scope = "chat" if session_key else "all"
        self._state["last_run"] = {
            "date": today,
            "at": started.isoformat(),
            "trigger": trigger,
            "scope": scope,
            "status": result.status,
            "counts": _counts(result.changes),
            "conflicts": result.conflicts,
            "pre_commit": pre_commit,
            "commit": result.commit,
            "undone": False,
        }
        if result.commit:
            # An empty run later must not hide the last undoable one.
            self._state["last_commit_run"] = dict(self._state["last_run"])
        self._save_state()
        record = {
            "at": started.isoformat(),
            "trigger": trigger,
            "scope": scope,
            "status": result.status,
            "message": result.message,
            "changes": [asdict(change) for change in result.changes],
            "rejected": result.rejected,
            "conflicts": result.conflicts,
            "commit": result.commit,
            "model_output": raw_output[:20000],
        }
        name = f"run-{started.strftime('%Y%m%d-%H%M%S')}.json"
        self._write_text(os.path.join(self._dir, name), json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        self._write_text(self._state_path, json.dumps(self._state, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _write_text(path: str, text: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("failed to write dream file", extra={"event": "dream.write", "path": path})
