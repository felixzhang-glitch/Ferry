"""Long-term memory workspace.

Memory lives as NOTE-FORMAT markdown files under `MEMORY_DIR`, one file per
category declared in `MEMORY_CATEGORIES` (format: `app.memory_format`). In
conversation, writes happen only on an explicit user request and are done by
the agent following `skills/memory/SKILL.md`; offline consolidation is done by
`app.dream`, which validates and applies its own edits.

This module:

1. Renders the block that gets injected into every turn (write protocol +
   resident categories + an index of the on-demand ones) from the parsed
   files, plus a JSON projection of all metadata (`memory-index.json`). The
   protocol has to be resident in pi's system prompt on every turn, rather
   than relying on a first-turn preamble that can disappear during native
   context compaction.
2. Keeps `MEMORY_DIR` under a local-only git repo (no remote) so a mistaken
   write stays reviewable and revertible. The git dir lives outside the
   worktree (`MEMORY_GIT_DIR`, under runtime/): an embedded `memory/.git`
   would make git treat memory/ as a nested-repo boundary and silently refuse
   to track `memory/README.md` in the main repo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

from app import memory_format
from app.config import Settings, get_settings
from app.memory_format import MemoryNote

logger = logging.getLogger(__name__)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_GIT_TIMEOUT_SECONDS = 15
_VALID_CATEGORY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_TRUNCATED_NOTE = "[记忆已截断，完整内容见 memory/]"

_CATEGORY_LABELS = {
    "basic": "基础档案",
    "health": "健康与身体数据",
    "preference": "偏好",
    "work": "工作与项目",
    "finance": "投资与理财",
    "recent": "近况",
    "social": "人际关系",
}

_CATEGORY_HINTS = {
    "basic": "身份与基础信息：生日、身高、常驻地、住址、办公地点等。",
    "health": "身体与健康数据：体重、运动、作息、身体状况。体重这类时序数据保留历史趋势。",
    "preference": "个人偏好：口味、审美、工具习惯、生活方式。",
    "work": "工作与项目：岗位、职责、在做的项目、技术栈。",
    "finance": "投资与理财：仓位、风险偏好、标的关注。",
    "recent": "近况：最近在做什么、在关注什么。过期条目定期归档。",
    "social": "人际关系：家人、朋友、同事的相关信息。",
}

# Write semantics hint for the agent and Dream: timeseries keeps history, state overwrites.
_CATEGORY_NATURE = {
    "basic": "state",
    "health": "timeseries",
    "preference": "state",
    "work": "state",
    "finance": "timeseries",
    "recent": "timeseries",
    "social": "state",
}

_PROTOCOL = """## 记忆写入协议

你拥有长期记忆，存放在项目根的 `memory/` 目录（每个类别一个 NOTE-FORMAT markdown 文件）。

触发条件（严格遵守）：
- 只有用户明确表达记忆意图时才写入，例如"记住/记一下/存下来/以后都/别忘了"。
- 日常对话中顺带提到的事实不要主动记录，离线的 Dream 会定期从对话里整理长期记忆。
- 用户要求查看、修改、删除记忆时，同样直接操作这些文件。

硬约束：
- 只能写下面列出的类别文件，禁止新建其他记忆文件；不要改动文件开头 `---` 之间的 frontmatter。
- 禁止修改 `rules/system.md` 与 `rules/admin.md`，那是人工维护的权威设定。
- 写入前先读目标文件，判断是新增条目还是更新已有条目。
- 条目格式 `- [key] @YYYY-MM-DD 内容 (source: user)`：key 是小写英文短词（如 weight、city、coffee），同一主题沿用已有 key；日期用本轮注入的当前时间；内容里不要用半角括号。
- 时序事实（体重、仓位、近况）追加带日期的新条目，保留历史趋势；状态事实（住址、设备、口味偏好）原地覆盖旧条目。
- 删除采用软删除：把条目移到文件末尾的 `## 已归档` 小节，并在行尾括号里追加 `; archived: YYYY-MM-DD`，不要物理删除。
- 写入后必须在回复里回执：`已记入 memory/<类别>.md：<条目原文>`。

完整操作规范见 `skills/memory/SKILL.md`，需要细节时自行读取。"""


def _resolve(path: str) -> str:
    """Resolve a configured path against the project root, not the CWD."""
    expanded = os.path.expanduser(path or "")
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(_ROOT, expanded))


def _settings_or_default(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def memory_dir(settings: Settings | None = None) -> str:
    return _resolve(_settings_or_default(settings).memory_dir)


def _git_dir(settings: Settings) -> str:
    return _resolve(settings.memory_git_dir)


def category_path(category: str, settings: Settings | None = None) -> str:
    return os.path.join(memory_dir(settings), f"{category}.md")


def label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category)


def nature(category: str) -> str:
    return _CATEGORY_NATURE.get(category, "state")


def hint(category: str) -> str:
    return _CATEGORY_HINTS.get(category, "由 Ferry 在用户明确要求或 Dream 整理时写入。")


def categories(settings: Settings | None = None) -> list[str]:
    return _categories(_settings_or_default(settings))


def index_path(settings: Settings | None = None) -> str:
    settings = _settings_or_default(settings)
    return os.path.join(os.path.dirname(_resolve(settings.memory_context_path)), "memory-index.json")


def _categories(settings: Settings) -> list[str]:
    valid: list[str] = []
    for name in settings.memory_category_list:
        if _VALID_CATEGORY.match(name):
            valid.append(name)
        else:
            logger.warning(
                "ignoring invalid memory category",
                extra={"event": "memory.config", "category": name},
            )
    return valid


def _resident(settings: Settings, categories: list[str]) -> list[str]:
    """Intersect the inject whitelist with the declared categories."""
    known = set(categories)
    resident: list[str] = []
    unknown: list[str] = []
    for name in settings.memory_always_inject_list:
        if name in known:
            resident.append(name)
        else:
            unknown.append(name)
    if unknown:
        logger.warning(
            "MEMORY_ALWAYS_INJECT contains categories missing from MEMORY_CATEGORIES",
            extra={"event": "memory.config", "unknown": ",".join(unknown)},
        )
    return resident


def _read_category(category: str, settings: Settings) -> str:
    try:
        with open(category_path(category, settings), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def read_text(category: str, settings: Settings | None = None) -> str:
    return _read_category(category, _settings_or_default(settings))


def load_notes(settings: Settings | None = None) -> dict[str, MemoryNote]:
    """Parse every declared category; a missing file parses as an empty note."""
    settings = _settings_or_default(settings)
    return {name: memory_format.parse_note(_read_category(name, settings)) for name in _categories(settings)}


def _skeleton(category: str) -> str:
    return memory_format.skeleton(
        category=category, title=label(category), nature=nature(category), description=hint(category)
    )


def _is_skeleton(text: str) -> bool:
    """A file without observations or relations carries no real memory yet."""
    note = memory_format.parse_note(text)
    return not note.active and not note.relations


def _render_section(name: str, note: MemoryNote) -> str:
    lines = [memory_format.inject_line(obs) for obs in note.active]
    lines.extend(f"- {rel.raw}" for rel in note.relations)
    return f"### {label(name)}（{name}）\n" + "\n".join(lines)


def build_index(settings: Settings | None = None, notes: dict[str, MemoryNote] | None = None) -> dict:
    """JSON projection of every category: frontmatter, observations, counts, lint."""
    settings = _settings_or_default(settings)
    notes = notes if notes is not None else load_notes(settings)
    resident = set(_resident(settings, list(notes)))
    view: dict[str, dict] = {}
    for name, note in notes.items():
        entry = memory_format.project_note(name, note)
        entry.update(label=label(name), path=category_path(name, settings), resident=name in resident)
        view[name] = entry
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "categories": view}


def render_memory_block(
    settings: Settings | None = None, notes: dict[str, MemoryNote] | None = None
) -> str:
    """Build the memory block injected into every turn.

    `MEMORY_MAX_INJECT_CHARS` bounds the memory content, not the fixed write
    protocol, which always ships in full. Returns an empty string when memory is
    disabled or nothing is configured, so callers can skip injection instead of
    adding noise to the context.
    """
    settings = _settings_or_default(settings)
    if not settings.memory_enabled:
        return ""

    categories = _categories(settings)
    if not categories:
        return ""
    if notes is None:
        notes = load_notes(settings)

    resident = _resident(settings, categories)
    limit = max(500, settings.memory_max_inject_chars)

    resident_sections: list[str] = []
    empty_resident: list[str] = []
    for name in resident:
        note = notes.get(name) or memory_format.parse_note("")
        if not note.active and not note.relations:
            # Still advertise it, otherwise the agent cannot know it may write here.
            empty_resident.append(name)
            continue
        resident_sections.append(_render_section(name, note))

    index_lines: list[str] = []
    for name in categories:
        if name in resident:
            continue
        count = len(notes[name].active) if name in notes else 0
        index_lines.append(
            f"- {label(name)}（{name}）: {category_path(name, settings)}（{count} 条）"
        )

    if not resident_sections and not index_lines and not empty_resident:
        return ""

    index_block = ""
    if index_lines:
        index_block = "\n".join(
            ["## 按需读取的记忆类别", "", "需要时用文件读取工具自行打开：", *index_lines]
        )

    empty_block = ""
    if empty_resident:
        names = "、".join(f"{label(name)}（{name}）" for name in empty_resident)
        empty_block = (
            f"## 暂无记录的记忆类别\n\n{names}\n\n"
            f"用户明确要求时写入 {memory_dir(settings)}/<类别>.md。"
        )

    parts = [_PROTOCOL]
    truncated = False

    # The protocol is fixed overhead and always ships intact -- a half-cut rule
    # set is worse than none -- so the budget only bounds the memory content.
    if resident_sections:
        used = len(index_block) + len(empty_block)
        kept: list[str] = []
        for section in resident_sections:
            # Always keep the first section so memory never silently vanishes;
            # drop the rest at category boundaries once the budget is spent.
            if kept and used + len(section) > limit:
                truncated = True
                break
            used += len(section)
            kept.append(section)
        body = "\n\n".join(kept)
        if len(body) > limit:
            # Safety net for a single category larger than the whole budget.
            body = body[:limit].rstrip()
            truncated = True
        parts.append("## 已记录的长期记忆\n\n" + body)

    if index_block:
        parts.append(index_block)
    if empty_block:
        parts.append(empty_block)
    if truncated:
        parts.append(_TRUNCATED_NOTE)
    return "\n\n".join(parts)


def _atomic_write(target: str, text: str) -> bool:
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp_path = f"{target}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, target)
    except OSError:
        return False
    return True


def write_category(category: str, text: str, settings: Settings | None = None) -> bool:
    return _atomic_write(category_path(category, _settings_or_default(settings)), text)


def write_context_file(settings: Settings | None = None) -> str | None:
    """Render the memory block to disk and return its path for injection.

    Commits any pending agent writes first, so each turn also snapshots the
    previous turn's changes. The metadata projection is refreshed alongside.
    """
    settings = _settings_or_default(settings)
    if not settings.memory_enabled:
        return None

    auto_commit(settings)

    notes = load_notes(settings)
    index_target = index_path(settings)
    index_text = json.dumps(build_index(settings, notes), ensure_ascii=False, indent=2)
    if not _atomic_write(index_target, f"{index_text}\n"):
        logger.warning(
            "failed to write memory index file",
            extra={"event": "memory.index_write", "path": index_target},
        )

    block = render_memory_block(settings, notes)
    if not block:
        return None

    target = _resolve(settings.memory_context_path)
    if not _atomic_write(target, f"{block}\n"):
        logger.warning(
            "failed to write memory context file",
            extra={"event": "memory.context_write", "path": target},
        )
        return None
    return target


def _run_git(directory: str, git_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", git_dir, "--work-tree", directory, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def git_available(settings: Settings | None = None) -> bool:
    return os.path.isdir(_git_dir(_settings_or_default(settings)))


def head(settings: Settings | None = None) -> str | None:
    settings = _settings_or_default(settings)
    if not git_available(settings):
        return None
    try:
        result = _run_git(memory_dir(settings), _git_dir(settings), "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def commit(settings: Settings | None, message: str) -> str | None:
    """Commit every pending change under memory/ and return the new HEAD.

    Returns None when nothing changed or git is unusable. Never raises: a
    broken git setup must not take the conversation down.
    """
    settings = _settings_or_default(settings)
    directory = memory_dir(settings)
    git_dir = _git_dir(settings)
    if not os.path.isdir(git_dir):
        return None

    try:
        status = _run_git(directory, git_dir, "status", "--porcelain")
        if status.returncode != 0 or not status.stdout.strip():
            return None
        added = _run_git(directory, git_dir, "add", "-A")
        if added.returncode != 0:
            raise RuntimeError(added.stderr.strip())
        committed = _run_git(directory, git_dir, "commit", "-m", message)
        if committed.returncode != 0:
            raise RuntimeError(committed.stderr.strip() or committed.stdout.strip())
        logger.info(
            "memory snapshot committed",
            extra={"event": "memory.git_commit", "changes": len(status.stdout.strip().splitlines())},
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.warning(
            "memory git commit failed",
            extra={"event": "memory.git_commit", "error": str(exc)},
        )
        return None
    return head(settings)


def revert(settings: Settings | None, sha: str) -> tuple[bool, str]:
    """Revert one memory commit; on conflict abort and leave the tree untouched."""
    settings = _settings_or_default(settings)
    directory = memory_dir(settings)
    git_dir = _git_dir(settings)
    if not os.path.isdir(git_dir):
        return False, "未启用记忆快照仓"
    try:
        result = _run_git(directory, git_dir, "revert", "--no-edit", sha)
        if result.returncode == 0:
            return True, head(settings) or ""
        _run_git(directory, git_dir, "revert", "--abort")
        return False, (result.stderr.strip() or result.stdout.strip())[:300]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def auto_commit(settings: Settings | None = None) -> None:
    """Snapshot pending memory changes into the local-only git repo."""
    settings = _settings_or_default(settings)
    if not settings.memory_git_auto_commit:
        return
    commit(settings, f"memory: auto snapshot {time.strftime('%Y-%m-%d %H:%M:%S')}")


def _ensure_git_repo(directory: str, settings: Settings) -> None:
    if not settings.memory_git_auto_commit:
        return
    git_dir = _git_dir(settings)

    # Migrate a legacy embedded repo: memory/.git makes the main repo treat
    # memory/ as a nested-repo boundary and refuse to track memory/README.md.
    legacy = os.path.join(directory, ".git")
    if os.path.isdir(legacy) and not os.path.isdir(git_dir):
        try:
            os.makedirs(os.path.dirname(git_dir), exist_ok=True)
            os.rename(legacy, git_dir)
            logger.info(
                "migrated embedded memory git dir out of the worktree",
                extra={"event": "memory.git_migrate", "path": git_dir},
            )
        except OSError as exc:
            logger.warning(
                "failed to migrate embedded memory git dir",
                extra={"event": "memory.git_migrate", "error": str(exc)},
            )
            return

    if os.path.isdir(git_dir):
        auto_commit(settings)
        return

    try:
        os.makedirs(os.path.dirname(git_dir), exist_ok=True)
        initialized = subprocess.run(
            ["git", "init", "--separate-git-dir", git_dir, directory],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if initialized.returncode != 0:
            raise RuntimeError(initialized.stderr.strip())
        # `--separate-git-dir` leaves a .git pointer file in the worktree,
        # which still marks a nested-repo boundary for the main repo -- drop it
        # and rely on explicit --git-dir/--work-tree invocations instead.
        pointer = os.path.join(directory, ".git")
        if os.path.isfile(pointer):
            os.remove(pointer)
        # Deliberately no remote: memory must never be pushable.
        logger.info(
            "initialized local-only memory git repo",
            extra={"event": "memory.git_init", "path": git_dir},
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.warning(
            "failed to initialize memory git repo",
            extra={"event": "memory.git_init", "error": str(exc)},
        )
        return
    auto_commit(settings)


def ensure_workspace(settings: Settings | None = None) -> None:
    """Create the memory directory, category skeletons and the local git repo.

    Idempotent: existing files are never overwritten.
    """
    settings = _settings_or_default(settings)
    if not settings.memory_enabled:
        return

    directory = memory_dir(settings)
    try:
        os.makedirs(directory, exist_ok=True)
        for name in _categories(settings):
            path = category_path(name, settings)
            if os.path.exists(path):
                continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_skeleton(name))
    except OSError as exc:
        logger.warning(
            "failed to prepare memory workspace",
            extra={"event": "memory.workspace", "path": directory, "error": str(exc)},
        )
        return

    _ensure_git_repo(directory, settings)
