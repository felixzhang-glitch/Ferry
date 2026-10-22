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

from app import console, mailer, memory
from app.config import get_settings
from app.console_api import build_router as build_console_router
from app.chat_api import build_router as build_chat_router
from app.web_chat import WebChatService, VisibleText
from app.web_chat_store import WebChatStore, ChatError, MAX_OUTPUT_BYTES
from app.dream import DreamRunner
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
console_defaults = console.baseline(settings)
console_overrides = console.load_overrides(settings)
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ferry", version="0.13.0")

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


async def send_web_reminder(thread_id: str, text: str, trace_id: str) -> None:
    if web_chat_service.store is None:
        raise RuntimeError("Web chat unavailable")
    await asyncio.to_thread(web_chat_service.store.add_scheduled_message, thread_id, trace_id,
                            VisibleText().feed(text, final=True), kind="reminder", notify=web_chat_service.notifications_enabled)


reminder_scheduler = ReminderScheduler(callback=send_reminder, store_path=settings.reminder_store_path,
                                       web_callback=send_web_reminder)


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
    if channel == "email":
        content = text.strip() or "(空响应)"
        await mailer.send_text(settings, target_id, mailer.subject_for(content), content)
        return
    if channel != "wechat":
        raise ValueError(f"unsupported push channel: {channel}")

    content = normalize_reply_text(text) or "(空响应)"
    chunks = split_message_text(content, max_chars=settings.wechat_message_chunk_chars)
    base_url = settings.wechat_sidecar_base_url.rstrip("/")
    headers = _sidecar_auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            response = await client.post(f"{base_url}/send", json={"to": target_id, "text": chunk}, headers=headers)
            response.raise_for_status()


async def run_web_daily(prompt: str, session_key: str, trace_id: str) -> str:
    final = None

    async def completed(text: str):
        nonlocal final
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("Web daily output exceeds storage limit")
        final = text

    parts, size = [], 0
    stream = agent_client.chat_stream(messages=[{"role": "user", "content": prompt}], trace_id=trace_id,
                                     session_key=session_key, on_final_text=completed)
    try:
        async for piece in stream:
            size += len(piece.encode("utf-8"))
            if size > MAX_OUTPUT_BYTES:
                raise ValueError("Web daily output exceeds storage limit")
            parts.append(piece)
    finally:
        await stream.aclose()
    return final if final is not None else "".join(parts)


async def deliver_web_daily(thread_id: str, text: str, event_id: str, success: bool) -> None:
    if web_chat_service.store is None:
        raise RuntimeError("Web chat unavailable")
    await asyncio.to_thread(web_chat_service.store.add_scheduled_message, thread_id, event_id,
                            VisibleText().feed(text, final=True), kind="daily", notify=success and web_chat_service.notifications_enabled)


daily_scheduler = DailyTaskScheduler(
    run_callback=run_daily_prompt,
    push_callback=push_daily_result,
    store_path=settings.daily_task_store_path,
    web_run_callback=run_web_daily,
    web_push_callback=deliver_web_daily,
)
dream_runner = DreamRunner(settings, agent_client, push_callback=push_daily_result)
web_chat_service = WebChatService(None, agent_client, session_manager, task_registry, dream_runner)
web_chat_service.reminders = reminder_scheduler
web_chat_service.daily = daily_scheduler
feishu_handler = FeishuWebhookHandler(
    settings=settings,
    feishu_client=feishu_client,
    agent_client=agent_client,
    session_manager=session_manager,
    deduplicator=deduplicator,
    task_registry=task_registry,
    reminder_scheduler=reminder_scheduler,
    daily_scheduler=daily_scheduler,
    dream_runner=dream_runner,
)
wechat_handler = WeChatWebhookHandler(
    settings=settings,
    agent_client=agent_client,
    session_manager=session_manager,
    deduplicator=deduplicator,
    task_registry=task_registry,
    daily_scheduler=daily_scheduler,
    dream_runner=dream_runner,
)
console_service = console.Console(
    settings,
    defaults=console_defaults,
    overrides=console_overrides,
    dream_runner=dream_runner,
    agent_client=agent_client,
    daily_scheduler=daily_scheduler,
    reminder_scheduler=reminder_scheduler,
)


def _console_token() -> str:
    try:
        return get_observability_settings().console_token.strip()
    except Exception:  # noqa: BLE001 - a broken observability config only disables the console
        return ""


def _chat_token() -> str:
    try:
        observation = get_observability_settings()
        return observation.chat_token.strip() if observation.enabled else ""
    except Exception:
        return ""


app.include_router(build_console_router(console_service, _console_token))
app.include_router(build_chat_router(web_chat_service, _chat_token))


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


def _sidecar_auth_headers() -> dict[str, str]:
    """Bearer header shared with the sidecar's /send and /send_file routes."""
    token = settings.wechat_webhook_token.strip()
    return {"authorization": f"Bearer {token}"} if token else {}


async def _push_file_to_wechat(to: str, file_path: str, caption: str) -> dict[str, Any]:
    base_url = settings.wechat_sidecar_base_url.rstrip("/")
    headers = _sidecar_auth_headers()
    # Encrypting plus CDN transfer far exceeds the text-push timeout.
    async with httpx.AsyncClient(timeout=180.0) as client:
        if caption:
            caption_response = await client.post(f"{base_url}/send", json={"to": to, "text": caption}, headers=headers)
            caption_response.raise_for_status()
        response = await client.post(f"{base_url}/send_file", json={"to": to, "path": file_path}, headers=headers)
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
                "queue_web": sum(s.queue.qsize() for s in web_chat_service.queue._sessions.values()),
                "active_web_turns": web_chat_service.active_count,
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


async def _push_owner_valid(owner: str) -> bool | None:
    observation = get_observability_settings()
    host = observation.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    host = "[::1]" if host == "::1" else host
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(f"http://{host}:{observation.port}/internal/observability/push-owner",
                                        params={"owner": owner},
                                        headers={"Authorization": f"Bearer {observation.chat_token}"})
            response.raise_for_status()
            value = response.json().get("valid")
            return value if type(value) is bool else None
    except (httpx.HTTPError, ValueError):
        return None


async def _cancel_orphan_web_tasks() -> None:
    if web_chat_service.store is None:
        return
    for scheduler, tasks, target_name, id_name in (
        (reminder_scheduler, reminder_scheduler.list_reminders(), "chat_id", "reminder_id"),
        (daily_scheduler, daily_scheduler.list_tasks(), "target_id", "task_id"),
    ):
        for task in tasks:
            if task.channel != "web":
                continue
            try:
                await asyncio.to_thread(web_chat_service.store.get_thread, getattr(task, target_name))
            except ChatError as exc:
                if exc.status != 404:
                    raise
                await scheduler.cancel(getattr(task, id_name))


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
    if _chat_token():
        try:
            web_chat_service.store = await asyncio.to_thread(WebChatStore, settings.web_chat_store_dir)
        except Exception:
            logger.warning("web chat storage unavailable")
    if settings.web_push_enabled and web_chat_service.store is not None:
        try:
            from app.web_push import WebPush
            web_chat_service.push = await asyncio.to_thread(WebPush, settings.web_push_store_dir,
                settings.web_push_contact, web_chat_service.store, _push_owner_valid)
            web_chat_service.notifications_enabled = True
            web_chat_service.push.start()
        except Exception as exc:
            logger.warning("web push unavailable", extra={"error_code": type(exc).__name__})
    await agent_client.start()
    await reminder_scheduler.start()
    await daily_scheduler.start()
    await _cancel_orphan_web_tasks()
    await dream_runner.start()
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
    app.state.feishu_ws = feishu_ws


@app.on_event("shutdown")
async def shutdown_event() -> None:
    # Gate the WS thread before closing the agent: late events would otherwise
    # hit an already-closed client and answer with transport errors.
    ws = getattr(app.state, "feishu_ws", None)
    if ws is not None:
        ws.stop()
    memory.auto_commit(settings)
    await web_chat_service.close()
    await dream_runner.close()
    await daily_scheduler.close()
    await reminder_scheduler.close()
    if getattr(web_chat_service, "push", None) is not None:
        await web_chat_service.push.close()
    await feishu_client.close()
    await agent_client.close()
    sampler = getattr(app.state, "observability_sampler", None)
    if sampler is not None:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    await asyncio.to_thread(telemetry.shutdown)
