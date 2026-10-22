"""Ferry's subset of basic-memory NOTE-FORMAT for memory category files.

A category file is one entity: YAML frontmatter, then observations
`- [key] @YYYY-MM-DD content #tag (k: v; k: v)` and relations
`- rel [[Target]]`. Observations below the `## 已归档` heading are archived.
The file is the source of truth; `project_note` is a derived JSON-safe view.

Writes go through `patch_note`, which rewrites only the targeted lines so
hand-written comments and prose in a file survive machine edits verbatim.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

ARCHIVE_HEADING = "## 已归档"
OBSERVATIONS_HEADING = "## Observations"
DEFAULT_KEY = "fact"

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DATE = r"\d{4}-\d{2}-\d{2}"
_LIST_ITEM = re.compile(r"^\s*[-*]\s+(.*)$")
_OBSERVATION = re.compile(r"^\[([^\[\]()]+)\]\s+(.+)$")
_CHECKBOX = {" ", "x", "X", "-", "/", ">", "?"}
_TIMECODE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_LEGACY_ARCHIVED = re.compile(rf"^({_DATE})\s*归档$")
_LEGACY_INNER = re.compile(rf"^\[({_DATE})\]\s+(.+)$")
_QUALIFIER = re.compile(rf"^@(?:(effective|valid|occurred|due|mentioned):)?({_DATE})(?=\s|$)\s*")
_CONTEXT = re.compile(r"\s\(([^()]*)\)\s*$")
_TAG = re.compile(r"(?:^|\s)#([^\s#()]+)")
_TYPED_RELATION = re.compile(r'^(?:"([^"]+)"|([^\s\[\]"]+))\s+\[\[([^\[\]]+)\]\]')
_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")
_META_KEY = re.compile(r"^[a-z_]+$")


@dataclass
class Observation:
    key: str
    content: str
    line: int
    date: str = ""
    date_kind: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    archived: bool = False
    legacy: bool = False


@dataclass
class Relation:
    rel_type: str
    target: str
    line: int
    raw: str


@dataclass
class MemoryNote:
    lines: list[str]
    frontmatter: dict[str, Any]
    title: str
    observations: list[Observation]
    relations: list[Relation]
    observations_heading: int | None
    archive_heading: int | None
    warnings: list[str]
    body_start: int = 0

    @property
    def active(self) -> list[Observation]:
        return [obs for obs in self.observations if not obs.archived]

    @property
    def archived(self) -> list[Observation]:
        return [obs for obs in self.observations if obs.archived]


def parse_context(raw: str) -> tuple[dict[str, str], list[str]]:
    """Split `k: v; k: v` pairs out of a context group; keep anything else verbatim."""
    meta: dict[str, str] = {}
    extra: list[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition(":")
        key, value = key.strip(), value.strip()
        if sep and value and _META_KEY.match(key):
            meta[key] = value
        else:
            extra.append(part)
    return meta, extra


def _tags(content: str) -> list[str]:
    return _TAG.findall(content)


def _parse_observation(body: str, line: int, archived: bool) -> Observation | None:
    match = _OBSERVATION.match(body)
    if match is None:
        return None
    raw_category, rest = match.group(1), match.group(2).strip()
    category = raw_category.strip()
    if raw_category in _CHECKBOX or not category or _TIMECODE.match(category):
        return None

    if DATE_PATTERN.match(category):
        # Pre-NOTE-FORMAT line `- [YYYY-MM-DD] 内容`; its parentheses are prose.
        return Observation(DEFAULT_KEY, rest, line, date=category, tags=_tags(rest),
                           archived=archived, legacy=True)
    legacy_archive = _LEGACY_ARCHIVED.match(category)
    if legacy_archive:
        inner = _LEGACY_INNER.match(rest)
        date, content = (inner.group(1), inner.group(2).strip()) if inner else ("", rest)
        return Observation(DEFAULT_KEY, content, line, date=date,
                           meta={"archived": legacy_archive.group(1)}, tags=_tags(content),
                           archived=True, legacy=True)

    date = date_kind = ""
    qualifier = _QUALIFIER.match(rest)
    if qualifier:
        date_kind, date = qualifier.group(1) or "", qualifier.group(2)
        rest = rest[qualifier.end():].strip()
    meta: dict[str, str] = {}
    extra: list[str] = []
    context = _CONTEXT.search(rest)
    if context and rest[:context.start()].strip():
        meta, extra = parse_context(context.group(1))
        rest = rest[:context.start()].strip()
    if not rest:
        return None
    return Observation(category, rest, line, date=date, date_kind=date_kind, meta=meta,
                       extra=extra, tags=_tags(rest), archived=archived)


def _parse_relation(body: str, line: int) -> Relation | None:
    links = _WIKILINK.findall(body)
    if not links:
        return None
    typed = _TYPED_RELATION.match(body)
    if typed:
        rel_type = typed.group(1) or typed.group(2)
        tail = body[typed.end():].strip()
        # A tail that is not one context group makes the whole line a plain links_to.
        if not tail or (tail.startswith("(") and tail.endswith(")") and tail.count("(") == 1):
            return Relation(rel_type, typed.group(3).strip(), line, body)
    return Relation("links_to", links[0].strip(), line, body)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _split_frontmatter(lines: list[str]) -> tuple[dict[str, Any], int, list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, 0, []
    for end in range(1, len(lines)):
        if lines[end].strip() != "---":
            continue
        try:
            data = yaml.safe_load("\n".join(lines[1:end])) or {}
        except yaml.YAMLError:
            return {}, end + 1, ["frontmatter: invalid YAML"]
        if not isinstance(data, dict):
            return {}, end + 1, ["frontmatter: not a mapping"]
        return _json_safe(data), end + 1, []
    return {}, 0, ["frontmatter: missing closing ---"]


def parse_note(text: str) -> MemoryNote:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    frontmatter, body_start, warnings = _split_frontmatter(lines)

    title = ""
    observations: list[Observation] = []
    relations: list[Relation] = []
    observations_heading: int | None = None
    archive_heading: int | None = None
    in_archive = in_code = in_comment = False

    for index in range(body_start, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        if in_comment:
            in_comment = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            in_comment = "-->" not in stripped
            continue
        if stripped.startswith("# "):
            title = title or stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            in_archive = stripped == ARCHIVE_HEADING
            if in_archive and archive_heading is None:
                archive_heading = index
            if stripped.lower() == OBSERVATIONS_HEADING.lower() and observations_heading is None:
                observations_heading = index
            continue
        if stripped.startswith("#") or not stripped:
            continue

        item = _LIST_ITEM.match(lines[index])
        if item is None:
            warnings.append(f"line {index + 1}: prose outside observations is not indexed")
            continue
        body = item.group(1).strip()
        observation = _parse_observation(body, index, in_archive)
        if observation is not None:
            observations.append(observation)
            continue
        relation = None if in_archive else _parse_relation(body, index)
        if relation is not None:
            relations.append(relation)
            continue
        # A bare list item still holds memory the agent meant to keep; index it
        # under the default key rather than silently dropping it from injection.
        observations.append(Observation(DEFAULT_KEY, body, index, tags=_tags(body),
                                        archived=in_archive, legacy=True))

    for obs in observations:
        if obs.legacy:
            warnings.append(f"line {obs.line + 1}: legacy entry without [key] @date")

    return MemoryNote(
        lines=lines,
        frontmatter=frontmatter,
        title=str(frontmatter.get("title") or title),
        observations=observations,
        relations=relations,
        observations_heading=observations_heading,
        archive_heading=archive_heading,
        warnings=warnings,
        body_start=body_start,
    )


def sanitize_content(content: str) -> str:
    """One line, no trailing ASCII paren group that would parse as context."""
    text = " ".join(str(content).split())
    return text.replace("(", "（").replace(")", "）")


def format_observation(
    key: str,
    content: str,
    *,
    date: str = "",
    date_kind: str = "",
    meta: dict[str, str] | None = None,
    extra: list[str] | None = None,
) -> str:
    parts = [f"- [{key}]"]
    if date:
        parts.append(f"@{date_kind}:{date}" if date_kind else f"@{date}")
    parts.append(content)
    context = [*(extra or []), *(f"{k}: {v}" for k, v in (meta or {}).items() if v)]
    if context:
        parts.append(f"({'; '.join(context)})")
    return " ".join(parts)


def observation_line(obs: Observation, **meta_updates: str) -> str:
    meta = {**obs.meta, **meta_updates}
    content = sanitize_content(obs.content) if obs.legacy else obs.content
    return format_observation(obs.key, content, date=obs.date, date_kind=obs.date_kind,
                              meta=meta, extra=obs.extra)


def inject_line(obs: Observation) -> str:
    """Compact line for the prompt: provenance metadata stays in the file."""
    date = f" @{obs.date}" if obs.date else ""
    return f"- [{obs.key}]{date} {obs.content}"


def patch_note(
    note: MemoryNote,
    *,
    replace: dict[int, str] | None = None,
    archive: dict[int, str] | None = None,
    append: list[str] | None = None,
) -> str:
    """Rewrite targeted lines only.

    `replace` swaps a line in place, `archive` removes a line and files the
    given text under `## 已归档`, `append` adds new active observations after
    the last active one.
    """
    replace = replace or {}
    archive = archive or {}
    append = list(append or [])
    lines = note.lines

    insert_after: int | None = None
    trailing_blank = False
    active_lines = [obs.line for obs in note.active]
    if active_lines:
        insert_after = max(active_lines)
    elif note.observations_heading is not None:
        insert_after = note.observations_heading
    elif note.archive_heading is not None:
        insert_after = note.archive_heading - 1
        while insert_after >= 0 and not lines[insert_after].strip():
            insert_after -= 1
        trailing_blank = True
    elif lines:
        insert_after = len(lines) - 1

    archive_after: int | None = None
    if note.archive_heading is not None:
        archived_lines = [obs.line for obs in note.archived]
        archive_after = max(archived_lines) if archived_lines else note.archive_heading
    archived_text = [archive[index] for index in sorted(archive)]

    out: list[str] = []
    if append and insert_after is not None and insert_after < 0:
        out.extend([*append, ""] if trailing_blank else append)
        append = []
    for index, line in enumerate(lines):
        if index in replace:
            out.append(replace[index])
        elif index not in archive:
            out.append(line)
        if append and index == insert_after:
            out.extend([*append, ""] if trailing_blank else append)
            append = []
        if archived_text and index == archive_after:
            out.extend(archived_text)
            archived_text = []
    if append:
        out.extend(append)
    if archived_text:
        while out and not out[-1].strip():
            out.pop()
        out.extend(["", ARCHIVE_HEADING, "", *archived_text])
    return "\n".join(out) + "\n"


def render_frontmatter(data: dict[str, Any]) -> str:
    dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10_000)
    return f"---\n{dumped}---\n"


def skeleton(*, category: str, title: str, nature: str, description: str) -> str:
    frontmatter = render_frontmatter({
        "title": title,
        "type": "memory",
        "permalink": f"memory/{category}",
        "category": category,
        "nature": nature,
        "description": description,
    })
    return f"{frontmatter}\n# {title}\n\n{OBSERVATIONS_HEADING}\n"


def _observation_view(category: str, obs: Observation, ordinal: str) -> dict[str, Any]:
    return {
        "id": f"{category}:{ordinal}",
        "key": obs.key,
        "date": obs.date,
        "date_kind": obs.date_kind or ("valid" if obs.date else ""),
        "content": obs.content,
        "tags": obs.tags,
        "meta": obs.meta,
        "legacy": obs.legacy,
        "line": obs.line + 1,
    }


def project_note(category: str, note: MemoryNote) -> dict[str, Any]:
    """JSON-safe projection: ids are 1-based ordinals over active observations."""
    active = note.active
    archived = note.archived
    dates = [obs.date for obs in active if obs.date]
    return {
        "category": category,
        "title": note.title,
        "frontmatter": note.frontmatter,
        "observations": [_observation_view(category, obs, str(i)) for i, obs in enumerate(active, 1)],
        "archived": [_observation_view(category, obs, f"a{i}") for i, obs in enumerate(archived, 1)],
        "relations": [
            {"type": rel.rel_type, "target": rel.target, "line": rel.line + 1} for rel in note.relations
        ],
        "counts": {"active": len(active), "archived": len(archived), "relations": len(note.relations)},
        "last_date": max(dates) if dates else "",
        "warnings": note.warnings,
    }
