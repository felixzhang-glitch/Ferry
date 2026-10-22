"""Loopback-only console API on the main service.

Only the observability process calls this, with OBSERVABILITY_CONSOLE_TOKEN.
Every write is a pick from `app.console.SPECS` or a fixed action.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from app.console import Console, ConsoleError
from observability.security import constant_token_match, is_loopback

MAX_BODY = 4096
_ID = r"^[A-Za-z0-9_-]{1,64}$"


class SettingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: StrictStr = Field(max_length=64)
    value: StrictBool | StrictStr | list[StrictStr]


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: StrictStr = Field(max_length=64)


class DailyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    hour: StrictInt | None = Field(default=None, ge=0, le=23)
    minute: StrictInt | None = Field(default=None, ge=0, le=59)
    target: StrictStr | None = Field(default=None, max_length=200)


def build_router(console: Console, token: Callable[[], str]) -> APIRouter:
    router = APIRouter(prefix="/internal/console")

    def authorize(request: Request) -> None:
        expected = token()
        if not expected:
            raise HTTPException(503, "console disabled")
        if not is_loopback(request.client.host if request.client else None):
            raise HTTPException(403, "loopback only")
        scheme, _, candidate = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not constant_token_match(candidate.strip(), expected):
            raise HTTPException(401, "unauthorized")

    async def body(request: Request, model: type[BaseModel]) -> Any:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "请求过大")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "需要 JSON")
        try:
            return model.model_validate(json.loads(raw or b"{}"))
        except (json.JSONDecodeError, ValidationError):
            raise HTTPException(422, "请求参数无效") from None

    async def reply(message: str) -> JSONResponse:
        state = await asyncio.to_thread(console.state)
        return JSONResponse({"ok": True, "message": message, "state": state})

    async def run(action) -> JSONResponse:
        try:
            message = await action()
        except ConsoleError as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=422)
        return await reply(message)

    @router.get("/state", dependencies=[Depends(authorize)])
    async def state() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(console.state))

    @router.post("/settings", dependencies=[Depends(authorize)])
    async def set_setting(request: Request) -> JSONResponse:
        payload = await body(request, SettingBody)
        return await run(lambda: console.set(payload.key, payload.value))

    @router.post("/settings/reset", dependencies=[Depends(authorize)])
    async def reset_setting(request: Request) -> JSONResponse:
        payload = await body(request, ResetBody)
        return await run(lambda: console.reset(payload.key))

    @router.post("/dream/run", dependencies=[Depends(authorize)])
    async def dream_run() -> JSONResponse:
        async def action() -> str:
            return console.dream_run()
        return await run(action)

    @router.post("/dream/undo", dependencies=[Depends(authorize)])
    async def dream_undo() -> JSONResponse:
        return await run(console.dream_undo)

    @router.post("/daily/{task_id}", dependencies=[Depends(authorize)])
    async def daily_update(task_id: str, request: Request) -> JSONResponse:
        _check_id(task_id)
        payload = await body(request, DailyBody)
        return await run(lambda: console.daily_update(
            task_id, enabled=payload.enabled, hour=payload.hour, minute=payload.minute, target=payload.target))

    @router.delete("/daily/{task_id}", dependencies=[Depends(authorize)])
    async def daily_delete(task_id: str) -> JSONResponse:
        _check_id(task_id)
        return await run(lambda: console.daily_delete(task_id))

    @router.delete("/reminders/{reminder_id}", dependencies=[Depends(authorize)])
    async def reminder_cancel(reminder_id: str) -> JSONResponse:
        _check_id(reminder_id)
        return await run(lambda: console.reminder_cancel(reminder_id))

    return router


def _check_id(value: str) -> None:
    import re

    if not re.match(_ID, value):
        raise HTTPException(422, "请求参数无效")
