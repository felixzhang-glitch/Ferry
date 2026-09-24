from __future__ import annotations

import asyncio
import hmac
import logging
import os
import resource
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import memory
from app.config import get_settings
from app.logging import setup_logging
from channel.feishu.client import FeishuClient, FeishuClientError
from channel.feishu.formatting import normalize_reply_text, split_message_text
from channel.feishu.handler import FeishuWebhookHandler
from channel.feishu.ws_client import FeishuWsClient
from channel.wechat.handler import WeChatWebhookHandler
from core.agent.pi_cli import PiCliClient
from core.session.daily_scheduler import DailyTaskScheduler
from core.session.deduplicator import MessageDeduplicator
from core.session.manager import SessionManager
from core.session.reminder_scheduler import ReminderScheduler
from core.session.task_registry import ActiveTaskRegistry
from observability import telemetry
from observability.config import get_observability_settings

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ferry", version="0.8.0")

session_manager = SessionManager()
deduplicator = MessageDeduplicator(ttl_seconds=settings.deduplicate_ttl_seconds)
task_registry = ActiveTaskRegistry()
agent_client = PiCliClient(settings=settings)
feishu_client = FeishuClient(settings=settings)


async def send_reminder(chat_id: str, text: str, trace_id: str) -> None:
    try:
        await feishu_client.send_markdown(
            receive_id=chat_id,
            receive_id_type="chat_id",
            markdown=text,
            trace_id=trace_id,
            request_uuid=trace_id,
        )
    except FeishuClientError:
        logger.warning("markdown reminder failed, falling back to text", extra={"trace_id": trace_id})
        await feishu_client.send_text(
            receive_id=chat_id,
            receive_id_type="chat_id",
            text=text,
            trace_id=trace_id,
            request_uuid=trace_id,
        )


reminder_scheduler = ReminderScheduler(callback=send_reminder, store_path=settings.reminder_store_path)


async def run_daily_prompt(prompt: str, session_key: str, trace_id: str) -> str:
    return await agent_client.chat(
        messages=[{"role": "user", "content": prompt}],
        trace_id=trace_id,
        session_key=session_key,
    )


async def push_daily_result(channel: str, target_id: str, text: str, trace_id: str) -> None:
    if channel == "feishu":
        await send_reminder(chat_id=target_id, text=text, trace_id=trace_id)
        return

    content = normalize_reply_text(text) or "(空响应)"
    chunks = split_message_text(content, max_chars=settings.wechat_message_chunk_chars)
    base_url = settings.wechat_sidecar_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            response = await client.post(f"{base_url}/send", json={"to": target_id, "text": chunk})
            response.raise_for_status()


daily_scheduler = DailyTaskScheduler(
    run_callback=run_daily_prompt,
    push_callback=push_daily_result,
    store_path=settings.daily_task_store_path,
)
feishu_handler = FeishuWebhookHandler(
    settings=settings,
    feishu_client=feishu_client,
    agent_client=agent_client,
    session_manager=session_manager,
    deduplicator=deduplicator,
    task_registry=task_registry,
    reminder_scheduler=reminder_scheduler,
    daily_scheduler=daily_scheduler,
)
wechat_handler = WeChatWebhookHandler(
    settings=settings,
    agent_client=agent_client,
    session_manager=session_manager,
    deduplicator=deduplicator,
    task_registry=task_registry,
    daily_scheduler=daily_scheduler,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/feishu", deprecated=True)
async def feishu_webhook(request: Request) -> JSONResponse:
    """Legacy webhook endpoint. Use long-connection mode instead."""
    raw_body = await request.body()
    try:
        result = await feishu_handler.handle_webhook(headers=request.headers, raw_body=raw_body)
        return JSONResponse(content=result)
    except Exception as exc:
        status = getattr(exc, "status_code", 500)
        detail = getattr(exc, "detail", str(exc))
        if status >= 500:
            logger.exception("webhook failed")
        else:
            logger.warning("webhook rejected: %s", detail)
        return JSONResponse(status_code=status, content={"code": status, "msg": detail})


@app.post("/webhook/wechat")
async def wechat_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    try:
        result = await wechat_handler.handle_webhook(headers=request.headers, raw_body=raw_body)
        return JSONResponse(content=result)
    except Exception as exc:
        status = getattr(exc, "status_code", 500)
        detail = getattr(exc, "detail", str(exc))
        if status >= 500:
            logger.exception("wechat webhook failed")
        else:
            logger.warning("wechat webhook rejected: %s", detail)
        return JSONResponse(status_code=status, content={"code": status, "msg": detail})


def _feishu_receive_id_type(receive_id: str) -> str:
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("on_"):
        return "union_id"
    return "chat_id"


async def _push_file_to_wechat(to: str, file_path: str, caption: str) -> dict[str, Any]:
    base_url = settings.wechat_sidecar_base_url.rstrip("/")
    # Encrypting plus CDN transfer far exceeds the text-push timeout.
    async with httpx.AsyncClient(timeout=180.0) as client:
        if caption:
            caption_response = await client.post(f"{base_url}/send", json={"to": to, "text": caption})
            caption_response.raise_for_status()
        response = await client.post(f"{base_url}/send_file", json={"to": to, "path": file_path})
    if response.status_code != 200:
        raise RuntimeError(f"wechat sidecar {response.status_code}: {response.text[:300]}")
    payload = response.json()
    return payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}


@app.post("/push/file")
async def push_file(request: Request) -> JSONResponse:
    """Deliver a local file to a feishu or wechat conversation."""
    trace_id = uuid.uuid4().hex
    expected_token = settings.push_api_token.strip()
    if not expected_token:
        return JSONResponse(
            status_code=503,
            content={"code": 503, "msg": "push api disabled: PUSH_API_TOKEN is not configured"},
        )
    supplied_auth = request.headers.get("authorization", "")
    # compare_digest on str rejects non-ASCII, so compare encoded bytes.
    if not hmac.compare_digest(supplied_auth.encode("utf-8"), f"Bearer {expected_token}".encode("utf-8")):
        return JSONResponse(status_code=401, content={"code": 401, "msg": "invalid push token"})

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "invalid json body"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"code": 400, "msg": "body must be a json object"})

    channel = str(payload.get("channel") or "").strip().lower()
    to = str(payload.get("to") or "").strip()
    file_path = str(payload.get("path") or payload.get("file") or "").strip()
    caption = str(payload.get("caption") or payload.get("text") or "").strip()

    if channel not in {"feishu", "wechat"}:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "channel must be feishu or wechat"})
    if not to or not file_path:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "missing to/path"})

    candidate = Path(file_path).expanduser()
    if not candidate.is_file():
        return JSONResponse(status_code=404, content={"code": 404, "msg": f"file not found: {file_path}"})
    size = candidate.stat().st_size
    if size == 0:
        return JSONResponse(status_code=400, content={"code": 400, "msg": "refusing to send an empty file"})
    max_bytes = int(settings.push_file_max_mb) * 1024 * 1024
    if size > max_bytes:
        return JSONResponse(
            status_code=413,
            content={"code": 413, "msg": f"file too large: {size} bytes exceeds limit {max_bytes}"},
        )

    logger.info(
        "pushing file",
        extra={"trace_id": trace_id, "event": "push.file", "channel": channel, "path": str(candidate), "size": size},
    )
    try:
        if channel == "feishu":
            receive_id_type = str(payload.get("receive_id_type") or "").strip() or _feishu_receive_id_type(to)
            if caption:
                await feishu_client.send_text(
                    receive_id=to,
                    text=caption,
                    trace_id=trace_id,
                    receive_id_type=receive_id_type,
                )
            file_key = await feishu_client.upload_file(str(candidate), trace_id=trace_id)
            message_id = await feishu_client.send_file(
                receive_id=to,
                file_key=file_key,
                trace_id=trace_id,
                receive_id_type=receive_id_type,
                request_uuid=trace_id,
            )
            result: dict[str, Any] = {"channel": channel, "file_key": file_key, "message_id": message_id}
        else:
            result = {"channel": channel, **await _push_file_to_wechat(to, str(candidate), caption)}
        result["size"] = size
    except FeishuClientError as exc:
        logger.warning(
            "push file failed",
            extra={"trace_id": trace_id, "event": "push.file", "channel": channel, "error": str(exc)[:300]},
        )
        return JSONResponse(status_code=502, content={"code": 502, "msg": str(exc)[:300]})
    except Exception as exc:
        logger.exception("push file failed", extra={"trace_id": trace_id, "event": "push.file", "channel": channel})
        return JSONResponse(status_code=502, content={"code": 502, "msg": str(exc)[:300]})

    logger.info(
        "push file succeeded",
        extra={"trace_id": trace_id, "event": "push.file", "channel": channel, "size": size},
    )
    return JSONResponse(content={"code": 0, "data": result})


async def _sample_observability() -> None:
    expected = time.monotonic()
    while True:
        try:
            pool = agent_client._worker_pool
            workers = pool.workers if pool else []
            gauges = {
                "mode": "worker" if pool else "cli",
                "queue_feishu": sum(s.queue.qsize() for s in feishu_handler._message_queue._sessions.values()),
                "queue_wechat": sum(s.queue.qsize() for s in wechat_handler._message_queue._sessions.values()),
                "active_tasks": len(task_registry._tasks),
                "worker_total": len(workers),
                "worker_busy": sum(w.busy for w in workers),
                "worker_starting": pool.starting if pool else 0,
                "circuit_open": int(agent_client._circuit_open_until > time.monotonic()),
                "session_mappings": len(agent_client._session_ids),
                "daily_tasks": len(daily_scheduler._tasks),
                "reminders": len(reminder_scheduler._reminders),
                "pi_active": len(agent_client._active_processes),
                "cpu_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime,
                "event_loop_lag_seconds": max(0., time.monotonic() - expected),
            }
            try:
                gauges["rss_bytes"] = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                gauges["disk_free_bytes"] = shutil.disk_usage(".").free
            except (OSError, ValueError, IndexError):
                pass
            telemetry.update_runtime(**gauges)
        except Exception:
            # Optional monitoring cannot stop the message loop or disclose data.
            logger.warning("observability runtime sampling failed")
        expected = time.monotonic() + 2.
        await asyncio.sleep(2.)


@app.on_event("startup")
async def startup_event() -> None:
    try:
        observation_settings = get_observability_settings()
        if observation_settings.enabled:
            telemetry.configure(observation_settings)
            app.state.observability_sampler = asyncio.create_task(_sample_observability())
    except Exception:
        logger.warning("observability disabled: invalid configuration")
    memory.ensure_workspace(settings)
    await agent_client.start()
    await reminder_scheduler.start()
    await daily_scheduler.start()
    loop = asyncio.get_running_loop()
    feishu_ws = FeishuWsClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        handler=feishu_handler,
        loop=loop,
        bot_open_id=settings.feishu_bot_open_id,
        group_require_mention=settings.feishu_group_require_mention,
    )
    feishu_ws.start_in_thread()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    memory.auto_commit(settings)
    await daily_scheduler.close()
    await reminder_scheduler.close()
    await feishu_client.close()
    await agent_client.close()
    sampler = getattr(app.state, "observability_sampler", None)
    if sampler is not None:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    await asyncio.to_thread(telemetry.shutdown)
