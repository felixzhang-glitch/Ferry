from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import defaultdict, deque
from typing import Callable

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from observability.appearance import AppearanceError
from core.session.chat_contract import (
    AttachmentCreate, ID_PATTERN, MAX_CHAT_JSON_BYTES, MAX_UPLOAD_BYTES, ThreadCreate,
    ThreadRename, TurnBody, read_json, PushSubscribe, PushPresence,
)
from observability.security import require_session_csrf

logger = logging.getLogger(__name__)


class AppearancePatch(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=32)
    haptics_enabled: bool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if self.name is None and self.haptics_enabled is None:
            raise ValueError("No appearance change")
        return self


class ChatRateLimiter:
    def __init__(self):
        self.attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, identity: str, kind: str) -> bool:
        import time
        now = time.monotonic()
        for key, values in list(self.attempts.items()):
            while values and values[0] <= now - 600:
                values.popleft()
            if not values:
                del self.attempts[key]
        window, limit = {"turn": (60, 12), "mutation": (60, 30), "upload": (600, 20)}[kind]
        key = (identity, kind)
        if key not in self.attempts and len(self.attempts) >= 768:
            return False
        values = self.attempts[key]
        if sum(value > now - window for value in values) >= limit:
            return False
        values.append(now)
        return True


def build_router(settings, sessions, enabled: Callable, check_origin: Callable,
                 client_factory: Callable, appearance_store: Callable) -> tuple[APIRouter, Callable]:
    rates = ChatRateLimiter()

    def require_chat(request: Request):
        if request.headers.getlist("authorization") or not sessions.valid(request.cookies.get("obs_session")):
            raise HTTPException(401, "需要身份验证")
        enabled()
        if not settings.chat_token:
            raise HTTPException(503, "聊天服务未配置")
        require_session_csrf(request, settings.chat_token, purpose="chat",
                             check_origin=lambda: check_origin(request, settings), logger=logger)
        if request.method not in ("GET", "HEAD"):
            identity = owner(request)
            kind = "turn" if request.url.path.endswith("/turns") else "mutation"
            if ((request.method == "POST" and request.url.path.endswith("/attachments"))
                    or (request.method == "PUT" and request.url.path == "/api/observability/v1/chat/appearance/avatar")):
                kind = "upload"
            if not rates.allow(identity, kind):
                raise HTTPException(429, "操作过于频繁，请稍后重试", headers={"Retry-After": "60"})

    router = APIRouter(prefix="/api/observability/v1/chat", dependencies=[Depends(require_chat)])

    async def appearance_call(method: str, *args):
        try:
            store = appearance_store()
            return await asyncio.to_thread(getattr(store, method), *args)
        except AppearanceError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @router.get("/appearance")
    async def appearance():
        return await appearance_call("snapshot")

    @router.patch("/appearance")
    async def appearance_update(request: Request):
        data = await read_json(request, AppearancePatch)
        return await appearance_call("update", data.name, data.haptics_enabled)

    @router.put("/appearance/avatar")
    async def appearance_avatar(request: Request):
        try:
            store = appearance_store()
            return await store.set_avatar(request.stream(), request.headers.get("content-type", "").split(";", 1)[0].strip().lower())
        except AppearanceError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @router.delete("/appearance/avatar")
    async def appearance_avatar_reset():
        return await appearance_call("reset_avatar")

    @router.delete("/appearance")
    async def appearance_reset():
        return await appearance_call("reset")

    def owner(request: Request) -> str:
        return hashlib.sha256(request.cookies["obs_session"].encode()).hexdigest()

    def path_id(value: str) -> str:
        if not re.fullmatch(ID_PATTERN, value):
            raise HTTPException(422, "请求参数无效")
        return value

    def thread_path(thread_id: str) -> str:
        return f"/threads/{path_id(thread_id)}"

    async def cancel_owner(identity: str):
        if not settings.chat_token:
            return
        try:
            async with client_factory() as client:
                await client.post(settings.chat_upstream.rstrip("/") + "/internal/chat/v1/cancel-all",
                                  headers={"Authorization": f"Bearer {settings.chat_token}", "X-Chat-Owner": identity},
                                  timeout=10.0)
        except httpx.HTTPError:
            pass

    async def cancel_cookie(cookie: str | None):
        if cookie:
            await cancel_owner(hashlib.sha256(cookie.encode()).hexdigest())

    async def forward(request: Request, method: str, path: str, payload: dict | None = None,
                      *, streaming: bool = False, uploading: bool = False, download: bool = False,
                      params: dict | None = None):
        client = client_factory()
        response = None
        headers = {"Authorization": f"Bearer {settings.chat_token}", "X-Chat-Owner": owner(request),
                   "X-Chat-Expires": str(sessions.expires_at(request.cookies.get("obs_session")))}
        try:
            kwargs = {"params": params, "headers": headers}
            if uploading:
                headers["Content-Type"] = "application/octet-stream"
                kwargs["content"] = request.stream()
            elif payload is not None:
                kwargs["json"] = payload
            upstream = client.build_request(method, settings.chat_upstream.rstrip("/") + "/internal/chat/v1" + path, **kwargs)
            response = await client.send(upstream, stream=True)
            if not 200 <= response.status_code < 300:
                status = response.status_code if response.status_code in (404, 409, 413, 415, 422, 429, 503) else 502
                detail = {404: "对话或附件不存在", 409: "当前操作冲突，请停止回复或刷新后重试",
                          413: "内容过大", 415: "文件类型不支持", 422: "请求参数或文件内容无效",
                          429: "操作过于频繁，请稍后重试", 503: "聊天服务暂不可用"}.get(status, "主服务拒绝了请求")
                raise HTTPException(status, detail)
            if streaming or download:
                expected = "application/x-ndjson" if streaming else None
                if expected and not response.headers.get("content-type", "").startswith(expected):
                    raise HTTPException(502, "主服务响应无效")
                stream_response, stream_client = response, client
                response, client = None, None

                async def relay():
                    used = 0
                    try:
                        async for chunk in stream_response.aiter_bytes():
                            if not sessions.valid(request.cookies.get("obs_session")):
                                await cancel_owner(owner(request))
                                break
                            used += len(chunk)
                            if used > (2 * 1024 * 1024 if streaming else MAX_UPLOAD_BYTES):
                                break
                            yield chunk
                    except httpx.HTTPError:
                        if streaming:
                            yield '{"v":1,"type":"error","code":"unavailable","message":"连接已中断，请重试","retryable":true}\n'.encode("utf-8")
                    finally:
                        with anyio.CancelScope(shield=True):
                            await stream_response.aclose()
                            await stream_client.aclose()

                outgoing = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
                if download and "content-disposition" in stream_response.headers:
                    outgoing["Content-Disposition"] = stream_response.headers["content-disposition"]
                return StreamingResponse(relay(), media_type=stream_response.headers.get("content-type"), headers=outgoing)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 4 * 1024 * 1024:
                    raise HTTPException(502, "主服务响应过大")
                body.extend(chunk)
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                raise HTTPException(502, "主服务响应无效") from None
            if not isinstance(value, dict):
                raise HTTPException(502, "主服务响应无效")
            return JSONResponse(value)
        except httpx.HTTPError:
            raise HTTPException(503, "主服务不可达，请稍后重试") from None
        finally:
            with anyio.CancelScope(shield=True):
                if response is not None:
                    await response.aclose()
                if client is not None:
                    await client.aclose()

    @router.get("/commands")
    async def commands(request: Request):
        return await forward(request, "GET", "/commands")

    @router.get("/threads")
    async def threads(request: Request):
        return await forward(request, "GET", "/threads")

    @router.post("/threads")
    async def create(request: Request):
        data = await read_json(request, ThreadCreate)
        return await forward(request, "POST", "/threads", data.model_dump())

    @router.patch("/threads/{thread_id}")
    async def rename(thread_id: str, request: Request):
        data = await read_json(request, ThreadRename)
        return await forward(request, "PATCH", thread_path(thread_id), data.model_dump())

    @router.delete("/threads/{thread_id}")
    async def delete(thread_id: str, request: Request):
        return await forward(request, "DELETE", thread_path(thread_id))

    @router.post("/threads/{thread_id}/reset")
    async def reset(thread_id: str, request: Request):
        return await forward(request, "POST", thread_path(thread_id) + "/reset")

    @router.get("/threads/{thread_id}/messages")
    async def messages(thread_id: str, request: Request, limit: int = Query(50, ge=1, le=100), before: str | None = None):
        params = {"limit": limit}
        if before:
            params["before"] = path_id(before)
        return await forward(request, "GET", thread_path(thread_id) + "/messages", params=params)

    @router.post("/threads/{thread_id}/attachments")
    async def reserve(thread_id: str, request: Request):
        data = await read_json(request, AttachmentCreate)
        return await forward(request, "POST", thread_path(thread_id) + "/attachments", data.model_dump())

    @router.put("/threads/{thread_id}/attachments/{attachment_id}/content")
    async def upload(thread_id: str, attachment_id: str, request: Request):
        return await forward(request, "PUT", thread_path(thread_id) + f"/attachments/{path_id(attachment_id)}/content", uploading=True)

    @router.delete("/threads/{thread_id}/attachments/{attachment_id}")
    async def remove_attachment(thread_id: str, attachment_id: str, request: Request):
        return await forward(request, "DELETE", thread_path(thread_id) + f"/attachments/{path_id(attachment_id)}")

    @router.get("/threads/{thread_id}/attachments/{attachment_id}/content")
    async def download(thread_id: str, attachment_id: str, request: Request):
        return await forward(request, "GET", thread_path(thread_id) + f"/attachments/{path_id(attachment_id)}/content", download=True)

    @router.post("/threads/{thread_id}/turns")
    async def turns(thread_id: str, request: Request):
        data = await read_json(request, TurnBody, MAX_CHAT_JSON_BYTES)
        return await forward(request, "POST", thread_path(thread_id) + "/turns", data.model_dump(), streaming=True)

    @router.get("/threads/{thread_id}/active")
    async def active(thread_id: str, request: Request):
        return await forward(request, "GET", thread_path(thread_id) + "/active")

    @router.get("/threads/{thread_id}/turns/{turn_id}/events")
    async def resume(thread_id: str, turn_id: str, request: Request):
        return await forward(request, "GET", thread_path(thread_id) + f"/turns/{path_id(turn_id)}/events", streaming=True)

    @router.get("/notifications")
    async def notifications(request: Request):
        return await forward(request, "GET", "/notifications")

    @router.put("/notifications/subscription")
    async def subscribe_push(request: Request):
        data = await read_json(request, PushSubscribe)
        return await forward(request, "PUT", "/notifications/subscription", data.model_dump())

    @router.delete("/notifications/subscription")
    async def unsubscribe_push(request: Request):
        return await forward(request, "DELETE", "/notifications/subscription")

    @router.post("/notifications/test")
    async def test_push(request: Request):
        return await forward(request, "POST", "/notifications/test")

    @router.post("/notifications/presence")
    async def presence(request: Request):
        data = await read_json(request, PushPresence)
        return await forward(request, "POST", "/notifications/presence", data.model_dump())

    @router.post("/threads/{thread_id}/stop")
    async def stop(thread_id: str, request: Request):
        return await forward(request, "POST", thread_path(thread_id) + "/stop")

    return router, cancel_cookie
