import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from channel.feishu.client import FeishuClient, FeishuClientError, resolve_feishu_file_type


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(
        feishu_api_base="https://open.feishu.cn",
        feishu_app_id="cli_xxx",
        feishu_app_secret="secret",
        feishu_tenant_token_url="https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        feishu_send_message_url="https://open.feishu.cn/open-apis/im/v1/messages",
        feishu_file_upload_url="https://open.feishu.cn/open-apis/im/v1/files",
        push_file_max_mb=30,
        feishu_max_retries=0,
        feishu_retry_backoff_seconds=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("resume.pdf", "pdf"),
        ("RESUME.PDF", "pdf"),
        ("a.doc", "doc"),
        ("a.docx", "doc"),
        ("a.xls", "xls"),
        ("a.xlsx", "xls"),
        ("a.ppt", "ppt"),
        ("a.pptx", "ppt"),
        # mp4/opus need a duration field, so they must fall back to stream.
        ("a.mp4", "stream"),
        ("a.opus", "stream"),
        ("a.txt", "stream"),
        ("a.tar.gz", "stream"),
        ("noext", "stream"),
    ],
)
def test_resolve_feishu_file_type(name: str, expected: str) -> None:
    assert resolve_feishu_file_type(name) == expected


async def test_upload_file_posts_multipart_and_returns_file_key(tmp_path: Path) -> None:
    target = tmp_path / "report_202608.pdf"
    target.write_bytes(b"%PDF-1.4 fake body")

    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                status_code=200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        if request.url.path.endswith("/im/v1/files"):
            seen["body"] = request.content
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                status_code=200,
                json={"code": 0, "data": {"file_key": "file_v3_abc"}},
            )
        return httpx.Response(status_code=404, json={"code": 99999})

    client = FeishuClient(settings=make_settings())
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)

    file_key = await client.upload_file(str(target), trace_id="t1")
    await client.close()

    assert file_key == "file_v3_abc"
    assert seen["auth"] == "Bearer t-1"
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b'name="file_type"' in body
    # pdf suffix must map to the pdf file_type, not the stream fallback.
    assert b"\r\n\r\npdf\r\n" in body
    assert b'name="file_name"' in body
    assert b"report_202608.pdf" in body
    assert b"%PDF-1.4 fake body" in body


async def test_upload_file_falls_back_to_stream_for_unknown_suffix(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")

    seen: dict[str, bytes] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                status_code=200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        seen["body"] = request.content
        return httpx.Response(status_code=200, json={"code": 0, "data": {"file_key": "fk"}})

    client = FeishuClient(settings=make_settings())
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)

    await client.upload_file(str(target), trace_id="t1")
    await client.close()

    assert b"\r\n\r\nstream\r\n" in seen["body"]


async def test_upload_file_rejects_missing_path(tmp_path: Path) -> None:
    client = FeishuClient(settings=make_settings())
    with pytest.raises(FeishuClientError, match="file not found"):
        await client.upload_file(str(tmp_path / "nope.pdf"), trace_id="t1")
    await client.close()


async def test_upload_file_rejects_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.pdf"
    target.write_bytes(b"")

    client = FeishuClient(settings=make_settings())
    with pytest.raises(FeishuClientError, match="empty file"):
        await client.upload_file(str(target), trace_id="t1")
    await client.close()


async def test_upload_file_rejects_directory(tmp_path: Path) -> None:
    client = FeishuClient(settings=make_settings())
    with pytest.raises(FeishuClientError, match="file not found"):
        await client.upload_file(str(tmp_path), trace_id="t1")
    await client.close()


async def test_upload_file_rejects_oversized(tmp_path: Path) -> None:
    target = tmp_path / "big.pdf"
    target.write_bytes(b"x" * 2048)

    # push_file_max_mb=0 makes any non-empty file exceed the cap.
    client = FeishuClient(settings=make_settings(push_file_max_mb=0))
    with pytest.raises(FeishuClientError, match="too large"):
        await client.upload_file(str(target), trace_id="t1")
    await client.close()


async def test_upload_file_surfaces_api_error(tmp_path: Path) -> None:
    target = tmp_path / "a.pdf"
    target.write_bytes(b"%PDF")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                status_code=200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        return httpx.Response(status_code=400, json={"code": 230002, "msg": "invalid file_type"})

    client = FeishuClient(settings=make_settings())
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)

    with pytest.raises(FeishuClientError, match="upload failed"):
        await client.upload_file(str(target), trace_id="t1")
    await client.close()


async def test_send_file_payload_and_receive_id_type() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                status_code=200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        seen["body"] = json.loads(request.content.decode("utf-8"))
        seen["query"] = dict(request.url.params)
        return httpx.Response(
            status_code=200,
            json={"code": 0, "data": {"message_id": "om_file_1"}},
        )

    client = FeishuClient(settings=make_settings())
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)

    message_id = await client.send_file(
        receive_id="ou_abc",
        file_key="file_v3_abc",
        trace_id="t1",
        receive_id_type="open_id",
        request_uuid="uuid-1",
    )
    await client.close()

    assert message_id == "om_file_1"
    assert seen["query"] == {"receive_id_type": "open_id"}
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["msg_type"] == "file"
    assert body["receive_id"] == "ou_abc"
    assert body["uuid"] == "uuid-1"
    # content must be a JSON-encoded string, not a nested object.
    assert json.loads(body["content"]) == {"file_key": "file_v3_abc"}


async def test_send_file_defaults_to_chat_id() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                status_code=200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        seen["query"] = dict(request.url.params)
        return httpx.Response(status_code=200, json={"code": 0, "data": {"message_id": "om_2"}})

    client = FeishuClient(settings=make_settings())
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)

    await client.send_file(receive_id="oc_abc", file_key="fk", trace_id="t1")
    await client.close()

    assert seen["query"] == {"receive_id_type": "chat_id"}


async def test_send_file_noop_without_file_key() -> None:
    client = FeishuClient(settings=make_settings())
    assert await client.send_file(receive_id="oc_abc", file_key="", trace_id="t1") == ""
    await client.close()
