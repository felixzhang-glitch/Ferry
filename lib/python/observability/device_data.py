"""Cookie/console-CSRF gateway, never telemetry or model input."""
from __future__ import annotations

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from app.device_data_api import PUBLIC, INTERNAL, build_routes
from core.device_data_contract import DeviceRecord, strict_json

MAX_RESPONSE_BYTES = 1024 * 1024
SAFE_STATUSES = {400, 401, 403, 404, 409, 413, 415, 422, 429, 503}


def build_router(settings, require_console, client_factory):
    async def execute(operation, args, payload, raw, request):
        suffix = request.url.path[len(PUBLIC):]
        url = settings.console_upstream.rstrip("/") + INTERNAL + suffix
        headers = {"Authorization": "Bearer " + settings.console_token, "Accept-Encoding": "identity"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with client_factory() as client:
                async with client.stream(request.method, url, content=raw, params=list(request.query_params.multi_items()),
                                         headers=headers, follow_redirects=False) as response:
                    if response.status_code in SAFE_STATUSES:
                        # Only status is trusted; upstream errors may contain private inputs.
                        return JSONResponse({"detail": "device_data_request_rejected"}, status_code=response.status_code,
                                            headers={"Cache-Control": "no-store"})
                    if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise HTTPException(502, "invalid_upstream_response")
                    length = response.headers.get("content-length")
                    if length is not None and (int(length) < 0 or int(length) > MAX_RESPONSE_BYTES):
                        raise HTTPException(502, "upstream_response_limit")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise HTTPException(502, "upstream_response_limit")
                        body.extend(chunk)
            data = strict_json(bytes(body))
            if operation == "start":
                from app.device_data_store import identifier
                if set(data) != {"sync_id"}:
                    raise ValueError()
                identifier(data["sync_id"])
            elif operation == "summary":
                if not isinstance(data.get("items"), list) or len(data["items"]) > 6:
                    raise ValueError()
            elif operation == "records":
                if not isinstance(data.get("records"), list) or len(data["records"]) > 200:
                    raise ValueError()
                for record in data["records"]:
                    DeviceRecord.model_validate(record)
                if type(data.get("offset")) is not int or (data.get("next_offset") is not None and type(data["next_offset"]) is not int):
                    raise ValueError()
            elif data != {"ok": True}:
                raise ValueError()
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
        except httpx.HTTPError:
            raise HTTPException(503, "device_data_upstream_unavailable") from None
        except HTTPException:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise HTTPException(502, "invalid_upstream_response") from None

    return build_routes(PUBLIC, require_console, execute)
