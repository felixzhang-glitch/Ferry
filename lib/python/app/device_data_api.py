"""Strict device-data routes; body parsing is shared by both security layers."""
from __future__ import annotations

import asyncio
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError

from app.device_data_store import DeviceDataError, identifier, validate_start, METRICS
from core.device_data_contract import (
    Batch, DeleteData, SyncStart, SyncCommit, MAX_BATCH_BYTES, MAX_CONTROL_BYTES,
    MAX_RECORDS, strict_json, canonical,
)
from observability.security import constant_token_match, is_loopback

INTERNAL = "/internal/device-data/v1"
PUBLIC = "/api/observability/v1/device-data"
BATCH_PATH = re.compile(r"/syncs/[a-f0-9]{32}/batches/(?:0|[1-9][0-9]{0,2})\Z")


def batch_route(method: str, path: str, prefix: str = PUBLIC):
    if method != "PUT" or not path.startswith(prefix):
        return False
    suffix = path[len(prefix):]
    return bool(BATCH_PATH.fullmatch(suffix)) and int(suffix.rsplit("/", 1)[1]) < 256


async def bounded_body(request: Request, limit: int):
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "unsupported_encoding")
    lengths = request.headers.getlist("content-length")
    try:
        if len(lengths) > 1 or (lengths and (int(lengths[0]) < 0 or int(lengths[0]) > limit)):
            raise HTTPException(413, "body_limit")
    except ValueError:
        raise HTTPException(400, "invalid_length") from None
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise HTTPException(413, "body_limit")
        raw.extend(chunk)
    return bytes(raw)


class SafeDeviceRoute(APIRoute):
    """No input-bearing exception may escape to the main app's error renderer."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({"detail": "invalid_request"}, status_code=422)
            except Exception:
                response = JSONResponse({"detail": "device_data_unavailable"}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe

    @staticmethod
    def parse(raw, request, model):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "json_required")
        try:
            value = model.model_validate(strict_json(raw))
            if isinstance(value, SyncStart):
                validate_start(value)
            canonical(value.model_dump())  # Reject lone Unicode surrogates, without echoing them.
            return value
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise HTTPException(422, "invalid_request") from None


def build_routes(prefix, authorize, execute):
    router = APIRouter(prefix=prefix, dependencies=[Depends(authorize)], route_class=SafeDeviceRoute)

    async def run(request, operation, model=None, args=()):
        try:
            limit = MAX_BATCH_BYTES if operation == "batch" else MAX_CONTROL_BYTES
            raw = await bounded_body(request, limit)
            if len(request.url.query) > MAX_CONTROL_BYTES:
                raise HTTPException(422, "invalid_query")
            allowed = {"device_id", "kind", "offset", "limit"} if operation == "records" else ({"device_id"} if operation == "summary" else set())
            pairs = list(request.query_params.multi_items())
            if any(key not in allowed for key, _ in pairs) or len({key for key, _ in pairs}) != len(pairs):
                raise HTTPException(422, "invalid_query")
            payload = SafeDeviceRoute.parse(raw, request, model) if model else None
            if model is None and raw:
                raise HTTPException(422, "unexpected_body")
            if operation in {"summary", "records"}:
                device = identifier(request.query_params.get("device_id"))
                args = (device,)
                if operation == "records":
                    kind = request.query_params.get("kind")
                    if kind not in METRICS:
                        raise HTTPException(422, "invalid_kind")
                    page = []
                    for name, default, minimum, maximum in (("offset", "0", 0, MAX_RECORDS), ("limit", "100", 1, 200)):
                        text = request.query_params.get(name, default)
                        if not re.fullmatch(r"0|[1-9][0-9]{0,5}", text) or not minimum <= int(text) <= maximum:
                            raise HTTPException(422, "invalid_page")
                        page.append(int(text))
                    args = (device, kind, *page)
            elif operation in {"batch", "commit", "cancel"}:
                identifier(args[0])
                if operation == "batch":
                    if not re.fullmatch(r"0|[1-9][0-9]{0,2}", args[1]) or int(args[1]) >= 256:
                        raise HTTPException(422, "invalid_batch_id")
                    args = (args[0], int(args[1]))
            return await execute(operation, args, payload, raw, request)
        except DeviceDataError as exc:
            raise HTTPException(exc.status, exc.code) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "device_data_unavailable") from None

    @router.post("/syncs")
    async def start(request: Request):
        return await run(request, "start", SyncStart)

    @router.put("/syncs/{sync_id}/batches/{batch_id}")
    async def batch(sync_id: str, batch_id: str, request: Request):
        return await run(request, "batch", Batch, (sync_id, batch_id))

    @router.post("/syncs/{sync_id}/commit")
    async def commit(sync_id: str, request: Request):
        return await run(request, "commit", SyncCommit, (sync_id,))

    @router.delete("/syncs/{sync_id}")
    async def cancel(sync_id: str, request: Request):
        return await run(request, "cancel", args=(sync_id,))

    @router.get("/summary")
    async def summary(request: Request):
        return await run(request, "summary")

    @router.get("/records")
    async def records(request: Request):
        return await run(request, "records")

    @router.delete("/data")
    async def delete(request: Request):
        return await run(request, "delete", DeleteData)

    return router


def build_router(store, token):
    def authorize(request: Request):
        if not request.client or not is_loopback(request.client.host):
            raise HTTPException(403, "loopback_only")
        expected = token()
        if not expected:
            raise HTTPException(503, "device_data_disabled")
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not constant_token_match(values[0], "Bearer " + expected):
            raise HTTPException(401, "unauthorized")

    async def execute(operation, args, payload, raw, request):
        current = store()
        if current is None:
            raise HTTPException(503, "device_data_unavailable")
        if operation == "batch":
            args = (*args, payload, len(raw))
        elif payload is not None:
            args = (*args, payload)
        result = await asyncio.to_thread(getattr(current, operation), *args)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return build_routes(INTERNAL, authorize, execute)
