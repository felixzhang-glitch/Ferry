from __future__ import annotations

import os
from pathlib import Path

# Shared skill files are independent of which CLI is installed. Project skills
# take precedence; legacy global directories remain discovery sources only.
SKILL_ROOTS = (
    str(Path(__file__).resolve().parents[3] / "skills"),
    "~/.pi/agent/skills",
    "~/.agents/skills",
    "~/.claude/skills",
    "~/.codex/skills",
)


def build_skill_summary(*, limit: int = 80) -> str:
    if limit <= 0:
        return ""
    skills: dict[str, str] = {}
    for root in SKILL_ROOTS:
        root_path = os.path.expanduser(root)
        if not os.path.isdir(root_path):
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames.sort()
            if "SKILL.md" not in filenames:
                continue
            skill_path = os.path.join(dirpath, "SKILL.md")
            name, description = read_skill_metadata(skill_path)
            if not name:
                name = os.path.basename(dirpath)
            skills.setdefault(name, description)
            if len(skills) >= limit:
                break
        if len(skills) >= limit:
            break

    if not skills:
        return ""
    lines: list[str] = []
    for name, description in sorted(skills.items(), key=lambda item: item[0].lower()):
        if description:
            lines.append(f"- `{name}`: {_truncate(description, limit=180)}")
        else:
            lines.append(f"- `{name}`")
    return "\n".join(lines)


def read_skill_metadata(skill_path: str) -> tuple[str, str]:
    try:
        with open(skill_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return "", ""

    if not lines or lines[0].strip() != "---":
        return "", ""

    name = ""
    description = ""
    idx = 1
    while idx < len(lines):
        line = lines[idx].rstrip("\n")
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("name:"):
            name = _clean_yaml_value(stripped.split(":", 1)[1])
        elif stripped.startswith("description:"):
            raw_value = stripped.split(":", 1)[1].strip()
            if raw_value in {">", ">-", "|", "|-"}:
                idx += 1
                parts: list[str] = []
                while idx < len(lines):
                    next_line = lines[idx].rstrip("\n")
                    next_stripped = next_line.strip()
                    if next_stripped == "---":
                        idx -= 1
                        break
                    if next_line and not next_line.startswith((" ", "\t")):
                        idx -= 1
                        break
                    if next_stripped:
                        parts.append(next_stripped)
                    idx += 1
                description = " ".join(parts)
            else:
                description = _clean_yaml_value(raw_value)
        idx += 1

    return name, description


def _clean_yaml_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1].strip()
    return cleaned


def _truncate(text: str, limit: int) -> str:
    compact = " ".join(text.strip().split())
    return compact if len(compact) <= limit else f"{compact[:limit - 3]}..."
