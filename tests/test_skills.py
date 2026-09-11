from pathlib import Path

import pytest

from app import skills


@pytest.mark.parametrize("description", [
    '"飞书即时通讯：收发消息和管理群聊。"',
    "'飞书即时通讯：收发消息和管理群聊。'",
])
def test_read_skill_metadata_supports_quoted_description(tmp_path, description):
    path = tmp_path / "SKILL.md"
    path.write_text(f"---\nname: lark-im\ndescription: {description}\n---\n", encoding="utf-8")
    assert skills.read_skill_metadata(str(path)) == ("lark-im", "飞书即时通讯：收发消息和管理群聊。")


def test_summary_supports_multiline_description_and_project_precedence(tmp_path, monkeypatch):
    project = tmp_path / "project" / "demo"
    global_skill = tmp_path / "global" / "demo"
    for directory in (project, global_skill):
        directory.mkdir(parents=True)
    (project / "SKILL.md").write_text(
        "---\nname: demo\ndescription: >-\n  第一行\n  第二行\n---\n", encoding="utf-8"
    )
    (global_skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 不应覆盖项目\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(skills, "SKILL_ROOTS", (str(project.parent), str(global_skill.parent)))
    assert skills.build_skill_summary() == "- `demo`: 第一行 第二行"
    assert skills.build_skill_summary(limit=0) == ""


def test_summary_handles_missing_metadata_and_limits(tmp_path, monkeypatch):
    for name in ("zeta", "alpha"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "SKILL.md").write_text("# no metadata\n", encoding="utf-8")
    monkeypatch.setattr(skills, "SKILL_ROOTS", (str(tmp_path),))
    assert skills.build_skill_summary(limit=1) == "- `alpha`"
    assert skills.build_skill_summary() == "- `alpha`\n- `zeta`"
    assert skills.read_skill_metadata(str(tmp_path / "absent")) == ("", "")


def test_project_root_points_to_actual_skills():
    assert Path(skills.SKILL_ROOTS[0]) == Path(__file__).resolve().parents[1] / "skills"
