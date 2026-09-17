"""Tests for pi native multimodal input via `@file` positional arguments.

pi CLI officially supports `pi [options] [@files...] [messages...]`, where
each `@path` is loaded as an attachment (image/pdf/etc.) before the prompt
string. These tests verify `PiCliClient` inserts image paths in the right
position, dedupes and filters them, and preserves backward compatibility
when no images are supplied.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from core.agent.pi_cli import PiCliClient


def _make_settings(tmp_path, **overrides):
    values = dict(
        pi_cli_bin="pi",
        pi_model="bailian/qwen3.8-flash",
        pi_thinking="",
        pi_tools="",
        pi_agent_dir="",
        pi_api_key="",
        pi_offline=True,
        pi_approve_project=True,
        pi_timeout_seconds=300.0,
        pi_idle_timeout_seconds=120.0,
        pi_session_store_path=str(tmp_path / "server" / "pi-sessions.json"),
        pi_work_dir=str(tmp_path / "workdir"),
        pi_stream_read_limit_bytes=262144,
        pi_max_retries=0,
        pi_retry_backoff_seconds=0.0,
        pi_circuit_breaker_threshold=5,
        pi_circuit_breaker_cooldown_seconds=30,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def two_images(tmp_path):
    img1 = tmp_path / "img1.jpg"
    img2 = tmp_path / "img2.png"
    img1.write_bytes(b"fake-jpeg")
    img2.write_bytes(b"fake-png")
    return str(img1), str(img2)


def test_build_command_without_images_is_unchanged(tmp_path) -> None:
    client = PiCliClient(settings=_make_settings(tmp_path))

    command = client._build_command(session_id="abc123")

    assert command[:3] == ["pi", "--mode", "json"]
    assert not any(arg.startswith("@") for arg in command)


def test_build_command_inserts_at_file_args_before_prompt(tmp_path, two_images) -> None:
    img1, img2 = two_images
    client = PiCliClient(settings=_make_settings(tmp_path))

    command = client._build_command(session_id="abc123", image_paths=[img1, img2])
    command.append("用户消息")

    at_indices = [i for i, arg in enumerate(command) if arg.startswith("@")]
    prompt_index = command.index("用户消息")

    assert at_indices == [prompt_index - 2, prompt_index - 1]
    assert command[at_indices[0]] == f"@{img1}"
    assert command[at_indices[1]] == f"@{img2}"


def test_build_command_preserves_image_order(tmp_path, two_images) -> None:
    img1, img2 = two_images
    client = PiCliClient(settings=_make_settings(tmp_path))

    command = client._build_command(image_paths=[img2, img1])

    at_args = [arg for arg in command if arg.startswith("@")]
    assert at_args == [f"@{img2}", f"@{img1}"]


def test_resolve_image_paths_dedupes_preserving_order(tmp_path, two_images) -> None:
    img1, img2 = two_images

    resolved = PiCliClient._resolve_image_paths([img1, img2, img1, img2, img1])

    assert resolved == [img1, img2]


def test_resolve_image_paths_drops_missing_files(tmp_path, two_images) -> None:
    img1, _ = two_images
    missing = str(tmp_path / "does-not-exist.jpg")

    resolved = PiCliClient._resolve_image_paths([img1, missing])

    assert resolved == [img1]


def test_resolve_image_paths_expands_user_home(tmp_path, monkeypatch, two_images) -> None:
    img1, _ = two_images
    fake_home = os.path.dirname(img1)
    monkeypatch.setenv("HOME", fake_home)
    basename = os.path.basename(img1)

    resolved = PiCliClient._resolve_image_paths([f"~/{basename}"])

    assert resolved == [img1]


def test_resolve_image_paths_handles_none_and_empty(tmp_path) -> None:
    assert PiCliClient._resolve_image_paths(None) == []
    assert PiCliClient._resolve_image_paths([]) == []


def test_resolve_image_paths_converts_relative_to_absolute(tmp_path, two_images) -> None:
    img1, _ = two_images
    monkey_cwd = tmp_path
    relative = os.path.relpath(img1, monkey_cwd)
    original_cwd = os.getcwd()
    try:
        os.chdir(monkey_cwd)
        resolved = PiCliClient._resolve_image_paths([relative])
    finally:
        os.chdir(original_cwd)

    assert resolved == [img1]


def test_resolve_image_paths_drops_empty_strings(tmp_path, two_images) -> None:
    img1, _ = two_images

    resolved = PiCliClient._resolve_image_paths(["", img1, ""])

    assert resolved == [img1]
