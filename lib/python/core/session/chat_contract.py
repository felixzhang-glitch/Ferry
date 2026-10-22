from __future__ import annotations

import json
from typing import Annotated

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

ID_PATTERN = r"^[a-f0-9]{32}$"
MAX_CHAT_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ChatID = Annotated[str, Field(pattern=ID_PATTERN)]


class ThreadCreate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    title: str = Field(default="新对话", min_length=1, max_length=80)


class ThreadRename(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    title: str = Field(min_length=1, max_length=80)


class AttachmentCreate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    mime: str = Field(min_length=1, max_length=128)
    size: int = Field(ge=1, le=MAX_UPLOAD_BYTES)


class CancelTurn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    client_turn_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class PushKeys(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    p256dh: str = Field(min_length=16, max_length=100)
    auth: str = Field(min_length=16, max_length=100)


class PushSubscription(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    endpoint: str = Field(min_length=1, max_length=2048)
    expirationTime: float | None = None
    keys: PushKeys


class PushSubscribe(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    subscription: PushSubscription
    scheduled: bool = True


class PushPresence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    thread_id: ChatID | None = None
    finished_turn_id: ChatID | None = None


class TurnBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    client_turn_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    text: str = Field(default="", max_length=32000)
    attachment_ids: list[ChatID] = Field(default_factory=list, max_length=5)
    retry_of: ChatID | None = None

    @model_validator(mode="after")
    def valid_turn(self):
        if self.retry_of:
            if self.text or self.attachment_ids:
                raise ValueError("Retry content is server-owned")
        elif not self.text.strip() and not self.attachment_ids:
            raise ValueError("Empty turn")
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError("Duplicate attachment")
        return self


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("Duplicate key")
        out[key] = value
    return out


def _constant(value):
    raise ValueError("Invalid JSON number")


async def read_json(request: Request, model, limit: int = 4096):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请使用 application/json")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(413, "请求体过大")
        raw.extend(chunk)
    try:
        return model.model_validate(json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "请求参数无效") from None
