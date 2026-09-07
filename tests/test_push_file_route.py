import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import main
from channel.feishu.client import FeishuClientError


class FakeRequest:
    """Stands in for starlette Request so the route runs without app lifespan."""

    def __init__(self, payload: object, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    async def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeFeishuClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_text(self, receive_id: str, text: str, trace_id: str, **kwargs) -> str:
        self.calls.append(("send_text", receive_id, text, kwargs.get("receive_id_type")))
        return "om_text"

    async def upload_file(self, file_path: str, trace_id: str) -> str:
        self.calls.append(("upload_file", file_path))
        return "file_v3_test"

    async def send_file(self, receive_id: str, file_key: str, trace_id: str, **kwargs) -> str:
        self.calls.append(("send_file", receive_id, file_key, kwargs.get("receive_id_type")))
        return "om_file"


def install_settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    base = dict(
        push_api_token="tok-secret",
        push_file_max_mb=30,
        wechat_sidecar_base_url="http://127.0.0.1:8787",
    )
    base.update(overrides)
    monkeypatch.setattr(main, "settings", SimpleNamespace(**base))


def auth_headers(token: str = "tok-secret") -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def make_file(tmp_path: Path, name: str = "note.txt", content: bytes = b"hello") -> Path:
    target = tmp_path / name
    target.write_bytes(content)
    return target


@pytest.mark.parametrize(
    ("receive_id", "expected"),
    [
        ("ou_abc123", "open_id"),
        ("on_abc123", "union_id"),
        ("oc_abc123", "chat_id"),
        ("", "chat_id"),
        ("something-else", "chat_id"),
    ],
)
def test_feishu_receive_id_type_detection(receive_id: str, expected: str) -> None:
    assert main._feishu_receive_id_type(receive_id) == expected


async def test_push_file_disabled_without_token(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch, push_api_token="")
    target = make_file(tmp_path)

    response = await main.push_file(FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}, auth_headers()))

    assert response.status_code == 503
    assert "PUSH_API_TOKEN" in json.loads(response.body)["msg"]


async def test_push_file_rejects_wrong_token(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)

    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}, auth_headers("nope"))
    )

    assert response.status_code == 401


async def test_push_file_rejects_missing_auth_header(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)

    response = await main.push_file(FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}))

    assert response.status_code == 401


async def test_push_file_rejects_non_ascii_auth_header(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)

    # Must be a clean 401, not a TypeError escaping into a 500.
    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}, {"authorization": "Bearer tök\u00e9n"})
    )

    assert response.status_code == 401


async def test_push_file_rejects_invalid_json(monkeypatch) -> None:
    install_settings(monkeypatch)

    response = await main.push_file(FakeRequest(ValueError("bad json"), auth_headers()))

    assert response.status_code == 400


async def test_push_file_rejects_non_object_body(monkeypatch) -> None:
    install_settings(monkeypatch)

    response = await main.push_file(FakeRequest(["not", "an", "object"], auth_headers()))

    assert response.status_code == 400


@pytest.mark.parametrize("channel", ["", "slack", "FEISHUX", "feishu ", "wechat"])
async def test_push_file_channel_validation(monkeypatch, tmp_path, channel: str) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)
    monkeypatch.setattr(main, "_push_file_to_wechat", _noop_wechat_push())
    monkeypatch.setattr(main, "feishu_client", FakeFeishuClient())

    response = await main.push_file(
        FakeRequest({"channel": channel, "to": "dest", "path": str(target)}, auth_headers())
    )

    stripped = channel.strip().lower()
    if stripped in {"feishu", "wechat"}:
        assert response.status_code == 200
    else:
        assert response.status_code == 400
        assert "channel" in json.loads(response.body)["msg"]


@pytest.mark.parametrize(
    "payload",
    [
        {"channel": "wechat", "to": "u@im.wechat"},
        {"channel": "wechat", "path": "/tmp/x.txt"},
        {"channel": "wechat", "to": "", "path": "/tmp/x.txt"},
        {"channel": "wechat", "to": "u@im.wechat", "path": ""},
    ],
)
async def test_push_file_requires_to_and_path(monkeypatch, tmp_path, payload: dict) -> None:
    install_settings(monkeypatch)

    response = await main.push_file(FakeRequest(payload, auth_headers()))

    assert response.status_code == 400
    assert "to/path" in json.loads(response.body)["msg"]


async def test_push_file_missing_file_is_404(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)

    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(tmp_path / "ghost.pdf")}, auth_headers())
    )

    assert response.status_code == 404


async def test_push_file_rejects_empty_file(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path, content=b"")

    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}, auth_headers())
    )

    assert response.status_code == 400
    assert "empty" in json.loads(response.body)["msg"]


async def test_push_file_rejects_oversized(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch, push_file_max_mb=0)
    target = make_file(tmp_path, content=b"x" * 10)

    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}, auth_headers())
    )

    assert response.status_code == 413


def _noop_wechat_push():
    recorded: dict[str, object] = {}

    async def fake(to: str, file_path: str, caption: str) -> dict:
        recorded["to"] = to
        recorded["path"] = file_path
        recorded["caption"] = caption
        return {"file_name": Path(file_path).name, "size": 5, "client_id": "cid-1"}

    fake.recorded = recorded
    return fake


async def test_push_file_wechat_success(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)
    fake_push = _noop_wechat_push()
    monkeypatch.setattr(main, "_push_file_to_wechat", fake_push)

    response = await main.push_file(
        FakeRequest(
            {"channel": "WeChat", "to": "u@im.wechat", "path": str(target), "caption": "简历"},
            auth_headers(),
        )
    )

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["code"] == 0
    assert body["data"]["channel"] == "wechat"
    assert body["data"]["size"] == 5
    assert body["data"]["client_id"] == "cid-1"
    assert fake_push.recorded == {"to": "u@im.wechat", "path": str(target), "caption": "简历"}


async def test_push_file_wechat_upstream_failure_is_502(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)

    async def boom(to: str, file_path: str, caption: str) -> dict:
        raise RuntimeError("wechat sidecar 502: getuploadurl failed ret=-1")

    monkeypatch.setattr(main, "_push_file_to_wechat", boom)

    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "path": str(target)}, auth_headers())
    )

    assert response.status_code == 502
    assert "getuploadurl" in json.loads(response.body)["msg"]


async def test_push_file_feishu_success_detects_open_id(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path, name="resume.pdf", content=b"%PDF")
    fake_client = FakeFeishuClient()
    monkeypatch.setattr(main, "feishu_client", fake_client)

    response = await main.push_file(
        FakeRequest({"channel": "feishu", "to": "ou_abc", "path": str(target)}, auth_headers())
    )

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["data"]["file_key"] == "file_v3_test"
    assert body["data"]["message_id"] == "om_file"
    assert fake_client.calls == [
        ("upload_file", str(target)),
        ("send_file", "ou_abc", "file_v3_test", "open_id"),
    ]


async def test_push_file_feishu_caption_sent_before_file(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)
    fake_client = FakeFeishuClient()
    monkeypatch.setattr(main, "feishu_client", fake_client)

    response = await main.push_file(
        FakeRequest({"channel": "feishu", "to": "oc_abc", "path": str(target), "caption": "给你"}, auth_headers())
    )

    assert response.status_code == 200
    assert [call[0] for call in fake_client.calls] == ["send_text", "upload_file", "send_file"]
    assert fake_client.calls[0][2] == "给你"
    assert fake_client.calls[2][3] == "chat_id"


async def test_push_file_feishu_explicit_receive_id_type_wins(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)
    fake_client = FakeFeishuClient()
    monkeypatch.setattr(main, "feishu_client", fake_client)

    response = await main.push_file(
        FakeRequest(
            {"channel": "feishu", "to": "ou_abc", "path": str(target), "receive_id_type": "chat_id"},
            auth_headers(),
        )
    )

    assert response.status_code == 200
    assert fake_client.calls[-1][3] == "chat_id"


async def test_push_file_feishu_client_error_is_502(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)

    class FailingClient(FakeFeishuClient):
        async def upload_file(self, file_path: str, trace_id: str) -> str:
            raise FeishuClientError("feishu file upload failed: {'code': 230002}")

    monkeypatch.setattr(main, "feishu_client", FailingClient())

    response = await main.push_file(
        FakeRequest({"channel": "feishu", "to": "oc_abc", "path": str(target)}, auth_headers())
    )

    assert response.status_code == 502
    assert "230002" in json.loads(response.body)["msg"]


async def test_push_file_accepts_file_alias_key(monkeypatch, tmp_path) -> None:
    install_settings(monkeypatch)
    target = make_file(tmp_path)
    fake_push = _noop_wechat_push()
    monkeypatch.setattr(main, "_push_file_to_wechat", fake_push)

    response = await main.push_file(
        FakeRequest({"channel": "wechat", "to": "u@im.wechat", "file": str(target)}, auth_headers())
    )

    assert response.status_code == 200
    assert fake_push.recorded["path"] == str(target)
