"""Sandboxed startup tests: no real .env, installs, CLI, or service calls."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "bin/server"
RUN_APP = ROOT / "bin/run-app"


@pytest.fixture
def sandbox(tmp_path):
    venv_bin = tmp_path / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("# Test activation only\n")
    (venv_bin / "python").symlink_to(sys.executable)
    return tmp_path


def run_helpers(root, body, file_text="", **overrides):
    env_file = root / "settings.env"
    env_file.write_text(file_text, encoding="utf-8")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE="1", EXPECTED_PI="pi")
    env.update(overrides)
    script = f'''
source {shlex.quote(str(SERVER))}
ROOT_DIR={shlex.quote(str(root))}
ENV_FILE="$ROOT_DIR/settings.env"
TRACE_FILE="$ROOT_DIR/trace"
cd "$ROOT_DIR"
ensure_env_file() {{ [[ -f "$ENV_FILE" ]]; }}
command() {{
  if [[ "$1" == "-v" ]]; then
    printf 'check|%s\\n' "$2" >> "$TRACE_FILE"
    [[ "$2" == python3 || "$2" == "$EXPECTED_PI" ]]
  else
    builtin command "$@"
  fi
}}
python() {{
  [[ "$*" == '-m pip install '* ]] || return 99
  printf 'pip|%s\\n' "$*" >> "$TRACE_FILE"
}}
git() {{ printf 'git|%s\\n' "$*" >> "$TRACE_FILE"; }}
{body}
'''
    result = subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=15)
    assert env_file.read_text(encoding="utf-8") == file_text
    return result


@pytest.mark.parametrize("file_text,overrides,expected_bin", [
    ("", {}, "pi"),
    ('PI_CLI_BIN="/custom/pi cli" # comment\n', {"EXPECTED_PI": "/custom/pi cli"}, "/custom/pi cli"),
    ("PI_CLI_BIN='alternate-pi'\n", {"EXPECTED_PI": "alternate-pi"}, "alternate-pi"),
    ('PI_CLI_BIN="missing"\n', {"PI_CLI_BIN": "shell-pi", "EXPECTED_PI": "shell-pi"}, "shell-pi"),
    ('PI_CLI_BIN="missing"\n', {"PI_CLI_BIN": ""}, "pi"),
])
def test_prerequisites_only_check_pi(sandbox, file_text, overrides, expected_bin):
    result = run_helpers(sandbox, "ensure_prerequisites", file_text, **overrides)
    assert result.returncode == 0, result.stderr
    trace = (sandbox / "trace").read_text().splitlines()
    assert [line for line in trace if line.startswith("check|")] == ["check|python3", f"check|{expected_bin}"]
    assert f"git|-C {sandbox}/runtime/codex-workdir/pi init -q" in trace
    assert (sandbox / "runtime/codex-workdir/pi").is_dir()
    assert result.stdout == ""


def test_missing_pi_fails_without_leaking_config(sandbox):
    result = run_helpers(sandbox, "ensure_prerequisites", "PI_CLI_BIN='dummy-secret'\n")
    assert result.returncode != 0
    assert "pi CLI" in result.stdout
    assert "dummy-secret" not in result.stdout + result.stderr
    assert not (sandbox / "runtime").exists()


@pytest.mark.parametrize("file_text,overrides,relative_cwd", [
    ("", {}, "runtime/codex-workdir/pi"),
    ("CODEX_WORK_DIR='./legacy work/'\n", {}, "legacy work/pi"),
    ("PI_WORK_DIR='./final cwd'\nCODEX_WORK_DIR='./old'\n", {}, "final cwd"),
    ("PI_WORK_DIR='./file cwd'\n", {"PI_WORK_DIR": "./shell cwd"}, "shell cwd"),
    ("PI_WORK_DIR='./file cwd'\n", {"CODEX_WORK_DIR": "./old shell"}, "file cwd"),
    ("CODEX_WORK_DIR='./old file'\n", {"PI_WORK_DIR": "./new shell"}, "new shell"),
    ("CODEX_WORK_DIR='./old file'\n", {"CODEX_WORK_DIR": "./old shell/"}, "old shell/pi"),
    ("PI_WORK_DIR=''\nCODEX_WORK_DIR='./old'\n", {}, "."),
    ("CODEX_WORK_DIR=''\n", {}, "pi"),
    ("CODEX_WORK_DIR='/'\n", {}, "/pi"),
])
def test_cwd_resolution_matches_settings(sandbox, file_text, overrides, relative_cwd, monkeypatch):
    result = run_helpers(sandbox, "get_pi_work_dir", file_text, **overrides)
    assert result.returncode == 0, result.stderr
    expected = (sandbox / relative_cwd).resolve()
    assert result.stdout.strip() == str(expected)
    for key in list(os.environ):
        if key.upper() in ("PI_WORK_DIR", "CODEX_WORK_DIR"):
            monkeypatch.delenv(key)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(sandbox)
    assert Path(Settings(_env_file=sandbox / "settings.env").pi_work_dir).resolve() == expected
    assert not (sandbox / "runtime").exists()


@pytest.mark.parametrize("key", ["PI_WORK_DIR", "CODEX_WORK_DIR"])
def test_work_dir_expands_home_like_pi_client(sandbox, key):
    result = run_helpers(sandbox, "get_pi_work_dir", f"{key}='~/work'\n", HOME=str(sandbox))
    assert result.returncode == 0, result.stderr
    suffix = "work" if key == "PI_WORK_DIR" else "work/pi"
    assert result.stdout.strip() == str(sandbox / suffix)


def test_only_final_cwd_gets_git_boundary(sandbox):
    result = run_helpers(sandbox, "ensure_prerequisites", "CODEX_WORK_DIR='./legacy'\n")
    assert result.returncode == 0, result.stderr
    trace = (sandbox / "trace").read_text().splitlines()
    assert [line for line in trace if line.startswith("git|")] == [f"git|-C {sandbox}/legacy/pi init -q"]
    assert (sandbox / "legacy/pi").is_dir()


def test_existing_git_boundary_is_preserved(sandbox):
    work_dir = sandbox / "existing"
    work_dir.mkdir()
    boundary = work_dir / ".git"
    boundary.write_text("gitdir: /historical/worktree\n")
    result = run_helpers(sandbox, "ensure_prerequisites", "PI_WORK_DIR='./existing'\n")
    assert result.returncode == 0, result.stderr
    assert "git|" not in (sandbox / "trace").read_text()
    assert boundary.read_text() == "gitdir: /historical/worktree\n"


def test_sync_pi_models_copies_registry_and_warns_unregistered(sandbox):
    registry = sandbox / "conf/pi/models.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"providers": {"bailian": {"models": [{"id": "deepseek-v4.1-flash"}]}}}),
        encoding="utf-8",
    )
    result = run_helpers(
        sandbox,
        "sync_pi_models\nsync_pi_models\n",
        "PI_MODEL='bailian/not-registered'\n",
        HOME=str(sandbox),
    )
    assert result.returncode == 0, result.stderr
    target = sandbox / ".pi/agent/models.json"
    assert target.read_bytes() == registry.read_bytes()
    assert result.stdout.count("[INFO] pi models.json 已同步") == 1
    assert not list((sandbox / ".pi/agent").glob("models.json.bak.*"))
    assert "bailian/not-registered" in result.stderr
    assert "未在 conf/pi/models.json 注册" in result.stderr


def test_sync_pi_models_backs_up_before_replacing(sandbox):
    agent_dir = sandbox / ".pi/agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "models.json").write_text('{"providers": {}}\n', encoding="utf-8")
    registry = sandbox / "conf/pi/models.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"providers": {"bailian": {"models": [{"id": "deepseek-v4.1-flash"}]}}}),
        encoding="utf-8",
    )
    result = run_helpers(
        sandbox,
        "sync_pi_models\n",
        "PI_MODEL='bailian/deepseek-v4.1-flash'\n",
        HOME=str(sandbox),
    )
    assert result.returncode == 0, result.stderr
    assert (agent_dir / "models.json").read_bytes() == registry.read_bytes()
    backups = list(agent_dir.glob("models.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"providers": {}}\n'
    assert "未在 conf/pi/models.json 注册" not in result.stderr


def test_sync_pi_models_skips_cleanly_without_registry(sandbox):
    result = run_helpers(sandbox, "sync_pi_models; echo done\n", "", HOME=str(sandbox))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"
    assert not (sandbox / ".pi").exists()


def test_runner_syncs_pi_models_on_start():
    assert "sync_pi_models" in RUN_APP.read_text()


def test_load_env_preserves_overrides_quotes_and_literal_data(sandbox):
    file_text = """export PI_CLI_BIN = '/custom/pi cli' # comment
PI_WORK_DIR="./cwd with spaces"
SERVER_PORT=8081
DASHSCOPE_API_KEY='dummy-file-secret'
EMPTY_OVERRIDE=file-value
LITERAL='$(touch should-not-exist); `touch neither`'
MULTILINE="first
second"
"""
    body = """
load_env
load_env
"$(env_python)" - <<'PY'
import json, os
keys = ('PI_CLI_BIN', 'PI_WORK_DIR', 'SERVER_PORT', 'EMPTY_OVERRIDE', 'LITERAL', 'MULTILINE')
print(json.dumps({key: os.environ[key] for key in keys}))
assert os.environ['DASHSCOPE_API_KEY'] == 'dummy-shell-secret'
PY
"""
    result = run_helpers(sandbox, body, file_text, SERVER_PORT="9090", DASHSCOPE_API_KEY="dummy-shell-secret", EMPTY_OVERRIDE="")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "PI_CLI_BIN": "/custom/pi cli", "PI_WORK_DIR": "./cwd with spaces", "SERVER_PORT": "9090",
        "EMPTY_OVERRIDE": "", "LITERAL": "$(touch should-not-exist); `touch neither`", "MULTILINE": "first\nsecond",
    }
    assert "dummy-shell-secret" not in result.stdout + result.stderr
    assert "dummy-file-secret" not in result.stdout + result.stderr
    assert not (sandbox / "should-not-exist").exists()
    assert not (sandbox / "neither").exists()


def test_load_env_does_not_hide_parser_failure(sandbox):
    result = run_helpers(sandbox, "env_file_values() { return 7; }; load_env; echo unexpected-success")
    assert result.returncode == 7
    assert "unexpected-success" not in result.stdout


def test_control_paths_and_runner_python_are_unchanged(sandbox):
    result = run_helpers(sandbox, 'printf "%s\\n" "$APP_PID_FILE" "$APP_LOG_FILE"')
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(ROOT / "runtime/server/ferry.pid"), str(ROOT / "logs/ferry.log")]
    runner = RUN_APP.read_text()
    assert ".opencode" not in runner
    assert "/root/.local/bin" in runner
    assert "/root/.nvm/versions/node/v20.19.4/bin" in runner
    assert "exec /root/.pyenv/versions/3.10.13/bin/python -m uvicorn" in runner
    assert 'source "$ROOT_DIR/bin/server"' in runner
    assert "load_env" in runner


def test_example_only_exposes_pi_and_shared_configuration():
    from dotenv import dotenv_values

    values = dotenv_values(ROOT / "conf/.env.example")
    assert values["PI_WORK_DIR"] == "./runtime/codex-workdir/pi"
    assert values["GENERATED_IMAGES_DIR"] == "~/.codex/generated_images"
    assert not {"ACTIVE_BACKEND", "BACKEND_STATE_PATH", "MAX_HISTORY_ROUNDS"} & values.keys()
    assert not any(key.startswith(("CODEX_", "CLAUDE_", "QODERCLI_", "OPENCODE_")) for key in values)


@pytest.mark.parametrize("script", [SERVER, RUN_APP])
def test_shell_syntax(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
