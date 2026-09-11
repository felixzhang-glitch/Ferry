"""Pi-only settings and input-only migration; never load the real conf/.env."""

import os

import pytest

from app.config import Settings


MIGRATIONS = [
    ("PI_WORK_DIR", "CODEX_WORK_DIR", "/new/cwd", "/old/work", "/old/work/pi"),
    ("PI_STREAM_READ_LIMIT_BYTES", "CODEX_STREAM_READ_LIMIT_BYTES", 524288, 131072, 131072),
    ("PI_MAX_RETRIES", "CODEX_MAX_RETRIES", 4, 1, 1),
    ("PI_RETRY_BACKOFF_SECONDS", "CODEX_RETRY_BACKOFF_SECONDS", 2.5, 0.25, 0.25),
    ("PI_CIRCUIT_BREAKER_THRESHOLD", "CODEX_CIRCUIT_BREAKER_THRESHOLD", 8, 3, 3),
    ("PI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "CODEX_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 60, 10, 10),
    ("GENERATED_IMAGES_DIR", "CODEX_GENERATED_IMAGES_DIR", "/new/images", "/old/images", "/old/images"),
]


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    keys = {field.validation_alias.upper() for field in Settings.model_fields.values()}
    keys.update(old for _, old, *_ in MIGRATIONS)
    for key in list(os.environ):
        if key.upper() in keys or key.upper().startswith(("CODEX_", "CLAUDE_", "QODERCLI_", "OPENCODE_")):
            monkeypatch.delenv(key)


def test_defaults_preserve_existing_pi_sessions_and_images():
    settings = Settings(_env_file=None)
    assert settings.pi_work_dir == "./runtime/codex-workdir/pi"
    assert settings.generated_images_dir == "~/.codex/generated_images"
    assert settings.pi_cli_bin == "pi"
    assert settings.pi_stream_read_limit_bytes == 262144
    assert settings.pi_max_retries == 2
    assert settings.pi_retry_backoff_seconds == 1.0
    assert settings.pi_circuit_breaker_threshold == 5
    assert settings.pi_circuit_breaker_cooldown_seconds == 30
    assert settings.pi_timeout_seconds == 300.0  # Unchanged by this migration.


@pytest.mark.parametrize("new,old,new_value,old_value,expected", MIGRATIONS)
@pytest.mark.parametrize("source", ["init", "shell", "dotenv"])
def test_legacy_keys_are_input_only(new, old, new_value, old_value, expected, source, monkeypatch, tmp_path):
    kwargs = {"_env_file": None}
    if source == "init":
        kwargs[old] = old_value
    elif source == "shell":
        monkeypatch.setenv(old, str(old_value))
    else:
        env_file = tmp_path / "settings.env"
        env_file.write_text(f'{old}="{old_value}"\n', encoding="utf-8")
        kwargs["_env_file"] = env_file
    settings = Settings(**kwargs)
    assert getattr(settings, new.lower()) == expected
    assert not hasattr(settings, old.lower())
    assert old.lower() not in settings.model_dump()


@pytest.mark.parametrize("new,old,new_value,old_value,expected", MIGRATIONS)
@pytest.mark.parametrize("source", ["init", "shell", "dotenv", "new_file_old_shell", "new_shell_old_file"])
def test_new_keys_always_win(new, old, new_value, old_value, expected, source, monkeypatch, tmp_path):
    kwargs = {"_env_file": None}
    file_values = {}
    if source == "init":
        kwargs.update({new: new_value, old: old_value})
    elif source == "shell":
        monkeypatch.setenv(new, str(new_value))
        monkeypatch.setenv(old, str(old_value))
    elif source == "dotenv":
        file_values = {new: new_value, old: old_value}
    elif source == "new_file_old_shell":
        file_values = {new: new_value}
        monkeypatch.setenv(old, str(old_value))
    else:
        file_values = {old: old_value}
        monkeypatch.setenv(new, str(new_value))
    if file_values:
        env_file = tmp_path / "settings.env"
        env_file.write_text("".join(f'{key}="{value}"\n' for key, value in file_values.items()), encoding="utf-8")
        kwargs["_env_file"] = env_file
    assert getattr(Settings(**kwargs), new.lower()) == new_value


@pytest.mark.parametrize("new,old,new_value,old_value,expected", MIGRATIONS)
@pytest.mark.parametrize("legacy", [False, True])
def test_shell_overrides_same_key_in_dotenv(new, old, new_value, old_value, expected, legacy, monkeypatch, tmp_path):
    key = old if legacy else new
    env_file = tmp_path / "settings.env"
    env_file.write_text(f'{key}="{new_value}"\n', encoding="utf-8")
    monkeypatch.setenv(key, str(old_value))
    assert getattr(Settings(_env_file=env_file), new.lower()) == (expected if legacy else old_value)


@pytest.mark.parametrize("parent,expected", [("./work/", "./work/pi"), ("/", "/pi"), ("", "pi")])
def test_legacy_work_dir_is_a_parent(parent, expected):
    assert Settings(_env_file=None, CODEX_WORK_DIR=parent).pi_work_dir == expected


def test_new_lowercase_fields_and_explicit_empty_values_are_preserved():
    settings = Settings(_env_file=None, pi_work_dir="./final", CODEX_WORK_DIR="./old", pi_max_retries=0)
    assert settings.pi_work_dir == "./final"
    assert settings.pi_max_retries == 0
    assert Settings(_env_file=None, PI_WORK_DIR="", CODEX_WORK_DIR="./old").pi_work_dir == ""
    assert Settings(_env_file=None, GENERATED_IMAGES_DIR="", CODEX_GENERATED_IMAGES_DIR="./old").generated_images_dir == ""


def test_retired_backend_fields_are_not_exposed():
    retired = [
        "ACTIVE_BACKEND", "BACKEND_STATE_PATH", "MAX_HISTORY_ROUNDS",
        "CODEX_API_BASE", "CODEX_API_KEY", "CODEX_MODEL", "CODEX_CLI_BIN",
        "CODEX_PERMISSION_MODE", "CODEX_TIMEOUT_SECONDS",
        "CLAUDE_CLI_BIN", "CLAUDE_MODEL", "CLAUDE_PERMISSION_MODE", "CLAUDE_TIMEOUT_SECONDS",
        "QODERCLI_CLI_BIN", "QODERCLI_MODEL", "QODERCLI_PERMISSION_MODE", "QODERCLI_TIMEOUT_SECONDS",
        "OPENCODE_CLI_BIN", "OPENCODE_MODEL", "OPENCODE_AGENT", "OPENCODE_TIMEOUT_SECONDS",
        "OPENCODE_IDLE_TIMEOUT_SECONDS", "OPENCODE_SESSION_STORE_PATH",
    ]
    settings = Settings(_env_file=None, **dict.fromkeys(retired, "ignored"))
    for key in retired:
        assert not hasattr(settings, key.lower())
    assert not hasattr(settings, "codex_chat_completions_url")
    assert not any(name.startswith(("codex_", "claude_", "qodercli_", "opencode_")) for name in settings.model_dump())
