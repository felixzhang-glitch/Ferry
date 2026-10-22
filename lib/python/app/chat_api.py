from __future__ import annotations

import asyncio
import json
import re
from typing import Callable

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.commands import command_catalog
from app.web_chat_store import ChatError
from core.session.chat_contract import (
    AttachmentCreate, CancelTurn, ID_PATTERN, MAX_CHAT_JSON_BYTES, ThreadCreate, ThreadRename, TurnBody, read_json,
    PushSubscribe, PushPresence,
)
from observability.security import constant_token_match, is_loopback


def _id(value: str) -> str:
    if not re.fullmatch(ID_PATTERN, value):
        raise HTTPException(422, "请求参数无效")
    return value


def build_router(service, token: Callable[[], str]) -> APIRouter:
    async def authorize(request: Request):
        if not request.client or not is_loopback(request.client.host):
            raise HTTPException(403, "仅允许本机调用")
        expected = token()
        values = request.headers.getlist("authorization")
        scheme, _, candidate = values[0].partition(" ") if len(values) == 1 else ("", "", "")
        if not expected or scheme.lower() != "bearer" or not constant_token_match(candidate.strip(), expected):
            raise HTTPException(401, "需要身份验证")
        if service is None or service.store is None:
            raise HTTPException(503, "聊天服务未启用")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "不支持压缩请求体")

    router = APIRouter(prefix="/internal/chat/v1", dependencies=[Depends(authorize)])

    async def call(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except ChatError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

    def owner(request: Request) -> str:
        value = request.headers.get("x-chat-owner", "")
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise HTTPException(422, "请求参数无效")
        return value

    @router.get("/commands")
    async def commands():
        return {"commands": command_catalog("web")}

    @router.get("/threads")
    async def threads():
        return {"threads": await call(service.store.list_threads)}

    @router.post("/threads")
    async def create(request: Request):
        data = await read_json(request, ThreadCreate)
        return {"thread": await call(service.store.create_thread, data.title)}

    @router.patch("/threads/{thread_id}")
    async def rename(thread_id: str, request: Request):
        data = await read_json(request, ThreadRename)
        return {"thread": await call(service.store.rename_thread, _id(thread_id), data.title)}

    @router.delete("/threads/{thread_id}")
    async def delete(thread_id: str):
        try:
            return await service.delete(_id(thread_id))
        except ChatError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

    @router.post("/threads/{thread_id}/reset")
    async def reset(thread_id: str):
        try:
            return {"thread": await service.reset(_id(thread_id))}
        except ChatError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

    @router.get("/threads/{thread_id}/messages")
    async def messages(thread_id: str, limit: int = Query(50, ge=1, le=100), before: str | None = None):
        return await call(service.store.messages, _id(thread_id), limit, _id(before) if before else None)

    @router.post("/threads/{thread_id}/attachments")
    async def reserve(thread_id: str, request: Request):
        data = await read_json(request, AttachmentCreate)
        return {"attachment": await call(service.store.reserve_attachment, _id(thread_id), data.name, data.mime, data.size)}

    @router.put("/threads/{thread_id}/attachments/{attachment_id}/content")
    async def upload(thread_id: str, attachment_id: str, request: Request):
        try:
            attachment = await service.store.write_attachment(_id(thread_id), _id(attachment_id), request.stream())
            return {"attachment": attachment}
        except ChatError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None

    @router.delete("/threads/{thread_id}/attachments/{attachment_id}")
    async def remove_attachment(thread_id: str, attachment_id: str):
        await call(service.store.delete_attachment, _id(thread_id), _id(attachment_id))
        return {"ok": True}

    @router.get("/threads/{thread_id}/attachments/{attachment_id}/content")
    async def download(thread_id: str, attachment_id: str):
        path, metadata = await call(service.store.attachment, _id(thread_id), _id(attachment_id))
        return FileResponse(path, media_type=metadata["mime"], filename=metadata["name"],
                            content_disposition_type="inline" if metadata["kind"] == "image" else "attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/threads/{thread_id}/turns")
    async def turns(thread_id: str, request: Request):
        data = await read_json(request, TurnBody, MAX_CHAT_JSON_BYTES)
        try:
            events = await service.open_turn(_id(thread_id), data, owner(request))
        except ChatError as exc:
            return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

        async def encode():
            try:
                async for event in events:
                    yield json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await events.aclose()

        return StreamingResponse(encode(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.get("/threads/{thread_id}/active")
    async def active(thread_id: str):
        try:
            return await service.active(_id(thread_id))
        except ChatError as exc:
            raise HTTPException(exc.status, exc.message) from None

    @router.get("/threads/{thread_id}/turns/{turn_id}/events")
    async def resume(thread_id: str, turn_id: str, request: Request):
        try:
            events = await service.subscribe(_id(thread_id), _id(turn_id), owner(request))
        except ChatError as exc:
            raise HTTPException(exc.status, exc.message) from None

        async def encode():
            try:
                async for event in events:
                    yield json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await events.aclose()
        return StreamingResponse(encode(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def push_service():
        push = getattr(service, "push", None)
        if push is None:
            raise HTTPException(503, "通知服务尚未配置")
        return push

    @router.get("/notifications")
    async def notifications(request: Request):
        push = getattr(service, "push", None)
        return await call(push.status, owner(request)) if push else {"enabled": False, "subscribed": False}

    @router.put("/notifications/subscription")
    async def subscribe_push(request: Request):
        data = await read_json(request, PushSubscribe)
        try:
            expires = float(request.headers.get("x-chat-expires", ""))
        except ValueError:
            raise HTTPException(422, "登录期限无效") from None
        return await call(push_service().subscribe, owner(request), expires,
                          data.subscription.model_dump(), data.scheduled)

    @router.delete("/notifications/subscription")
    async def unsubscribe_push(request: Request):
        await call(push_service().unsubscribe, owner(request))
        return {"ok": True}

    @router.post("/notifications/presence")
    async def presence(request: Request):
        data = await read_json(request, PushPresence)
        await call(push_service().presence, owner(request), data.thread_id, data.finished_turn_id)
        return {"ok": True}

    @router.post("/notifications/test")
    async def test_push(request: Request):
        await call(push_service().test, owner(request))
        return {"ok": True}

    @router.post("/threads/{thread_id}/stop")
    async def stop(thread_id: str):
        return {"stopped": await service.stop(_id(thread_id))}

    @router.post("/threads/{thread_id}/cancel")
    async def cancel_turn(thread_id: str, request: Request):
        data = await read_json(request, CancelTurn)
        return {"stopped": await service.cancel_turn(_id(thread_id), data.client_turn_id, owner(request))}

    @router.post("/cancel-all")
    async def cancel(request: Request):
        identity = owner(request)
        if getattr(service, "push", None) is not None:
            await call(service.push.unsubscribe, identity)
        await service.cancel_all(identity)
        return {"ok": True}

    return router
