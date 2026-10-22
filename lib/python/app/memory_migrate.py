"""One-off migration of legacy memory files to NOTE-FORMAT.

Run from the repo root:

    PYTHONPATH=lib/python python3 -m app.memory_migrate           # dry run: print the diff
    PYTHONPATH=lib/python python3 -m app.memory_migrate --apply   # write, with git snapshots

The model only assigns a `key` to each legacy entry; dates and contents carry
over verbatim and the conversion aborts if that ever fails to hold. Keys from
the dry run are cached so `--apply` writes exactly the diff that was reviewed.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import os
import re
import sys
from collections import Counter

from app import memory, memory_format
from app.config import Settings, get_settings
from app.memory_format import DEFAULT_KEY, KEY_PATTERN, MemoryNote, Observation

_COMMENT = re.compile(r"^\s*<!--\s*(.*?)\s*-->\s*$")
_HINT_SUFFIX = re.compile(r"\s*条目格式\s*[:：].*$")
_HEADING_DATE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")

_KEY_INSTRUCTIONS = """你是数据整理助手，只做一件事：给每条个人记忆条目分配一个 key。

- key 是小写英文短词或下划线词组（[a-z][a-z0-9_]*，不超过 20 个字符），描述这条记忆是关于什么的，
  例如 birthday、height、city、address、weight、exercise、coffee、travel、position、risk_appetite、project。
- 同一类别里同一主题的条目必须用同一个 key（例如所有体重记录都用 weight）。
- 只输出一个 JSON 对象 {"<id>": "<key>", ...}，覆盖输入里的每个 id，不要输出任何其他内容。"""


def is_migrated(note: MemoryNote) -> bool:
    return note.frontmatter.get("type") == "memory"


def entry_id(category: str, obs: Observation) -> str:
    return hashlib.sha256(f"{category}\0{obs.date}\0{obs.content}".encode()).hexdigest()[:12]


def _fingerprint(note: MemoryNote) -> Counter:
    return Counter((memory_format.sanitize_content(obs.content), obs.archived) for obs in note.observations)


def _dated(note: MemoryNote) -> Counter:
    return Counter((memory_format.sanitize_content(obs.content), obs.date) for obs in note.observations if obs.date)


def convert(category: str, text: str, keys: dict[str, str]) -> str:
    """Rewrite one legacy file in place: entry lines become NOTE-FORMAT, the rest stays.

    Headings, comments and prose keep their position; an undated entry under a
    dated `## [YYYY-MM-DD] …` section heading inherits that date.
    """
    note = memory_format.parse_note(text)
    by_line = {obs.line: obs for obs in note.observations}
    title = note.title or memory.label(category)
    description = ""

    def line_for(obs: Observation, indent: str, inherited: str) -> str:
        key = obs.key if not obs.legacy else keys.get(entry_id(category, obs), DEFAULT_KEY)
        meta = {"source": obs.meta.get("source", "migrated")}
        meta.update({k: v for k, v in obs.meta.items() if k != "source"})
        content = memory_format.sanitize_content(obs.content) if obs.legacy else obs.content
        return indent + memory_format.format_observation(
            key, content, date=obs.date or inherited, date_kind=obs.date_kind, meta=meta, extra=obs.extra
        )

    body: list[str] = []
    section_date = ""
    needs_heading = note.observations_heading is None and bool(note.observations)
    for index in range(note.body_start, len(note.lines)):
        line = note.lines[index]
        stripped = line.strip()
        if index in by_line:
            if needs_heading:
                body.extend([memory_format.OBSERVATIONS_HEADING])
                needs_heading = False
            indent = line[: len(line) - len(line.lstrip())]
            body.append(line_for(by_line[index], indent, section_date))
            continue
        if stripped.startswith("## "):
            # The first observation heading only belongs before the first entry.
            needs_heading = False
            found = _HEADING_DATE.search(stripped)
            section_date = found.group(1) if found else ""
        comment = _COMMENT.match(line)
        if comment and "条目格式" in comment.group(1) and not description:
            description = _HINT_SUFFIX.sub("", comment.group(1)).strip()
            continue
        if not stripped and body and not body[-1].strip():
            continue
        body.append(line)

    frontmatter = {
        "title": title,
        "type": "memory",
        "permalink": f"memory/{category}",
        "category": category,
        "nature": memory.nature(category),
        "description": description or memory.hint(category),
    }
    frontmatter.update({k: v for k, v in note.frontmatter.items() if k != "type"})
    while body and not body[0].strip():
        body.pop(0)
    converted = memory_format.render_frontmatter(frontmatter) + "\n" + "\n".join(body).rstrip("\n") + "\n"

    migrated = memory_format.parse_note(converted)
    if _fingerprint(migrated) != _fingerprint(note) or _dated(note) - _dated(migrated):
        raise RuntimeError(f"{category}: converted entries differ from the original, aborting")
    return converted


def _parse_key_map(raw: str) -> dict[str, str]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str) and KEY_PATTERN.match(v)}


async def assign_keys(settings: Settings, entries: list[tuple[str, str, Observation]]) -> dict[str, str]:
    from core.agent.pi_cli import PiCliClient

    listing = "\n".join(f"{eid}\t{category}\t{obs.content}" for eid, category, obs in entries)
    prompt = f"每行是 id<TAB>类别<TAB>条目内容：\n{listing}"
    client = PiCliClient(settings)
    raw = await client.complete(
        prompt,
        system_prompts=[_KEY_INSTRUCTIONS],
        model=settings.dream_model.strip() or None,
        timeout_seconds=settings.dream_timeout_seconds,
    )
    return _parse_key_map(raw)


def _cache_path(settings: Settings) -> str:
    return os.path.join(os.path.dirname(memory.index_path(settings)), "memory-migrate-keys.json")


def _load_cache(settings: Settings) -> dict[str, str]:
    try:
        with open(_cache_path(settings), encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items() if isinstance(v, str) and KEY_PATTERN.match(v)}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def plan_migration(settings: Settings, *, use_llm: bool) -> dict[str, tuple[str, str]]:
    """category -> (old text, new text) for every file still in the legacy format."""
    pending: dict[str, tuple[str, MemoryNote]] = {}
    for category in memory.categories(settings):
        text = memory.read_text(category, settings)
        note = memory_format.parse_note(text)
        if text.strip() and not is_migrated(note):
            pending[category] = (text, note)

    keys = _load_cache(settings)
    missing = [
        (entry_id(category, obs), category, obs)
        for category, (_, note) in pending.items()
        for obs in note.observations
        if obs.legacy and entry_id(category, obs) not in keys
    ]
    if missing and use_llm:
        try:
            keys.update(asyncio.run(assign_keys(settings, missing)))
        except Exception as exc:  # noqa: BLE001 - fall back to the default key
            print(f"[warn] key assignment failed, using '{DEFAULT_KEY}': {exc}", file=sys.stderr)
    if keys:
        os.makedirs(os.path.dirname(_cache_path(settings)), exist_ok=True)
        with open(_cache_path(settings), "w", encoding="utf-8") as fh:
            json.dump(keys, fh, ensure_ascii=False, indent=2)

    return {category: (text, convert(category, text, keys)) for category, (text, _) in pending.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate memory/*.md to NOTE-FORMAT")
    parser.add_argument("--apply", action="store_true", help="write files (default: dry run)")
    parser.add_argument("--no-llm", action="store_true", help=f"use '{DEFAULT_KEY}' for every key")
    args = parser.parse_args(argv)

    settings = get_settings()
    plan = plan_migration(settings, use_llm=not args.no_llm)
    if not plan:
        print("nothing to migrate: every category is already NOTE-FORMAT")
        return 0
    for category, (old, new) in plan.items():
        path = memory.category_path(category, settings)
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
        ))
    if not args.apply:
        print(f"\ndry run: {len(plan)} file(s) would change; rerun with --apply to write them")
        return 0

    memory.commit(settings, "memory: pre-migrate snapshot")
    for category, (_, new) in plan.items():
        if not memory.write_category(category, new, settings):
            print(f"[error] failed to write {category}", file=sys.stderr)
            return 1
    sha = memory.commit(settings, "memory: migrate to NOTE-FORMAT")
    memory.write_context_file(settings)
    print(f"\nmigrated {len(plan)} file(s); memory commit {sha or '(git unavailable)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
