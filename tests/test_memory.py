import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from app import memory


def _make_settings(tmp_path, **overrides):
    categories = overrides.pop("categories", "basic,health,work")
    always = overrides.pop("always", "basic,health")
    settings = SimpleNamespace(
        memory_enabled=True,
        memory_dir=str(tmp_path / "memory"),
        memory_categories=categories,
        memory_always_inject=always,
        memory_max_inject_chars=4000,
        memory_git_auto_commit=False,
        memory_git_dir=str(tmp_path / "memory-git"),
        memory_context_path=str(tmp_path / "runtime" / "memory-context.md"),
        memory_category_list=[c.strip() for c in categories.split(",") if c.strip()],
        memory_always_inject_list=[c.strip() for c in always.split(",") if c.strip()],
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _write(settings, category: str, body: str) -> None:
    path = memory.category_path(category, settings)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def test_ensure_workspace_creates_skeletons_and_is_idempotent(tmp_path) -> None:
    settings = _make_settings(tmp_path)

    memory.ensure_workspace(settings)

    directory = memory.memory_dir(settings)
    assert sorted(os.listdir(directory)) == ["basic.md", "health.md", "work.md"]

    # A skeleton carries no real memory yet, so it must not be injected.
    assert memory._is_skeleton(open(memory.category_path("basic", settings), encoding="utf-8").read())

    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 身高 175cm\n")
    memory.ensure_workspace(settings)
    with open(memory.category_path("basic", settings), encoding="utf-8") as fh:
        assert "175cm" in fh.read()


def test_relative_memory_dir_resolves_against_project_root(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_dir="./memory")

    resolved = memory.memory_dir(settings)

    # CWD must not influence where memory lives.
    assert resolved == os.path.join(memory._ROOT, "memory")
    assert os.path.isabs(resolved)


def test_render_block_injects_resident_and_indexes_on_demand(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    memory.ensure_workspace(settings)
    _write(settings, "basic", "# 基础档案\n\n<!-- hint -->\n\n- [2026-07-30] 身高 175cm\n")
    _write(settings, "health", "# 健康\n\n- [2026-07-30] 体重 80kg\n")
    _write(settings, "work", "# 工作\n\n- [2026-07-30] 阿里云 TAM\n- [2026-07-30] RAG\n")

    block = memory.render_memory_block(settings)

    # Resident categories carry their full text.
    assert "175cm" in block
    assert "体重 80kg" in block
    # The file's own heading and hint comments are not duplicated into the block.
    assert "### 基础档案（basic）" in block
    assert "<!-- hint -->" not in block
    assert not any(line.startswith("# ") for line in block.splitlines())
    # Non-resident categories only expose a path plus an entry count.
    assert "阿里云 TAM" not in block
    assert memory.category_path("work", settings) in block
    assert "（2 条）" in block
    # The write protocol must be present so it survives past a session's first turn.
    assert "记忆写入协议" in block


def test_render_block_skips_empty_resident_but_still_advertises_it(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    memory.ensure_workspace(settings)
    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 身高 175cm\n")

    block = memory.render_memory_block(settings)

    assert "### 健康与身体数据（health）" not in block
    # Otherwise the agent could not know the category is writable.
    assert "暂无记录的记忆类别" in block
    assert "health" in block


def test_render_block_truncates_at_category_boundary(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_max_inject_chars=500)
    memory.ensure_workspace(settings)
    _write(settings, "basic", "# 基础档案\n\n" + "- [2026-07-30] 基础条目\n" * 40)
    _write(settings, "health", "# 健康\n\n- [2026-07-30] 体重 80kg\n")

    block = memory.render_memory_block(settings)

    assert memory._TRUNCATED_NOTE in block
    # The first resident category is kept; the one past the budget is dropped.
    assert "基础条目" in block
    assert "体重 80kg" not in block
    # The write protocol is fixed overhead and must survive truncation intact.
    assert block.startswith(memory._PROTOCOL)


def test_render_block_ignores_whitelist_entries_outside_schema(tmp_path) -> None:
    settings = _make_settings(tmp_path, always="basic,ghost")
    memory.ensure_workspace(settings)
    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 身高 175cm\n")

    block = memory.render_memory_block(settings)

    assert "175cm" in block
    assert "ghost" not in block


def test_render_block_drops_invalid_category_names(tmp_path) -> None:
    settings = _make_settings(tmp_path, categories="basic,../escape", always="basic")
    settings.memory_category_list = ["basic", "../escape"]
    memory.ensure_workspace(settings)
    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 身高 175cm\n")

    block = memory.render_memory_block(settings)

    assert "escape" not in block
    assert not os.path.exists(tmp_path / "escape.md")


def test_render_block_empty_when_disabled(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_enabled=False)
    memory.ensure_workspace(settings)

    assert memory.render_memory_block(settings) == ""
    assert memory.write_context_file(settings) is None


def test_write_context_file_roundtrip(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    memory.ensure_workspace(settings)
    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 身高 175cm\n")

    path = memory.write_context_file(settings)

    assert path == os.path.abspath(settings.memory_context_path)
    with open(path, encoding="utf-8") as fh:
        assert "175cm" in fh.read()
    # The temp file used for the atomic replace must not linger.
    assert not os.path.exists(f"{path}.tmp")


def test_auto_commit_snapshots_changes(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_git_auto_commit=True)
    memory.ensure_workspace(settings)
    directory = memory.memory_dir(settings)
    git_dir = settings.memory_git_dir

    assert os.path.isdir(git_dir)
    # No .git entry may remain inside the worktree: it would mark a nested-repo
    # boundary and block the main repo from tracking memory/README.md.
    assert not os.path.exists(os.path.join(directory, ".git"))
    # Memory must never be pushable.
    remotes = subprocess.run(
        ["git", "--git-dir", git_dir, "remote"], capture_output=True, text=True, check=True
    )
    assert remotes.stdout.strip() == ""

    def _count() -> int:
        result = subprocess.run(
            ["git", "--git-dir", git_dir, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())

    before = _count()

    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 体重 75kg\n")
    memory.auto_commit(settings)

    after = _count()
    assert after == before + 1

    # A clean tree produces no empty commit.
    memory.auto_commit(settings)
    assert _count() == after


def test_ensure_workspace_migrates_legacy_embedded_git(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_git_auto_commit=True)
    directory = memory.memory_dir(settings)
    os.makedirs(directory, exist_ok=True)
    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 迁移前条目\n")
    subprocess.run(["git", "-C", directory, "init"], capture_output=True, check=True)
    subprocess.run(["git", "-C", directory, "add", "-A"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", directory, "commit", "-m", "legacy"], capture_output=True, check=True
    )

    memory.ensure_workspace(settings)

    # The embedded repo moved out of the worktree with history intact.
    assert not os.path.exists(os.path.join(directory, ".git"))
    log = subprocess.run(
        ["git", "--git-dir", settings.memory_git_dir, "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "legacy" in log.stdout


def test_auto_commit_degrades_silently_without_git(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_git_auto_commit=True)
    memory.ensure_workspace(settings)

    with patch("app.memory._run_git", side_effect=OSError("git missing")):
        memory.auto_commit(settings)  # must not raise

    # An unusable git setup must not stop the memory block from rendering.
    _write(settings, "basic", "# 基础档案\n\n- [2026-07-30] 身高 175cm\n")
    with patch("app.memory._run_git", side_effect=OSError("git missing")):
        assert "175cm" in memory.render_memory_block(settings)


def test_auto_commit_skipped_when_disabled(tmp_path) -> None:
    settings = _make_settings(tmp_path, memory_git_auto_commit=False)
    memory.ensure_workspace(settings)

    assert not os.path.isdir(settings.memory_git_dir)
    with patch("app.memory._run_git") as run_git:
        memory.auto_commit(settings)
    run_git.assert_not_called()
