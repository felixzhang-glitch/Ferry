"""Standalone ASGI app. Run with proxy headers disabled; never imports app.main."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from observability.config import ObservabilitySettings, get_observability_settings
from observability.security import (
    LoginRateLimiter, SessionStore, constant_token_match, is_loopback, verify_password,
)

API = "/api/observability/v1"
MAX_BODY = 4096
STATIC_DIR = Path(__file__).resolve().parent / "static"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; base-uri 'none'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
COMMON_KEYS = ("schema_version", "window", "generated_at", "data_until", "quality")

# Deliberately lazy: importing this web app neither opens the store nor starts a
# collector. These two seams also allow isolated tests with no production data.
def _create_reader(settings: ObservabilitySettings):
    from observability.store import MetricReader
    return MetricReader(settings.store_dir, retention_days=settings.retention_days)


def _create_collector(settings: ObservabilitySettings):
    from observability.telemetry import Collector
    return Collector(settings.store_dir, producer="sidecar", retention_days=settings.retention_days)


def _create_usage_ledger(settings: ObservabilitySettings):
    from observability.pi_usage import PiUsageLedger
    if not settings.hmac_key:
        raise ValueError("pi usage requires a pseudonymization key")
    return PiUsageLedger(settings.store_dir, settings.resolved_pi_session_dirs,
                         settings.hmac_key, sync_interval_seconds=settings.pi_scan_interval_seconds)


def _usage_dates(period: str, start: str | None, end: str | None) -> tuple[str | None, str | None]:
    if period == "custom":
        if not start or not end:
            raise HTTPException(422, "自定义日期需要开始和结束日期")
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            raise HTTPException(422, "日期格式应为 YYYY-MM-DD") from None
        if first > last or (last - first).days > 3660:
            raise HTTPException(422, "日期顺序无效或自定义范围超过十年")
        return first.isoformat(), last.isoformat()
    if start is not None or end is not None:
        raise HTTPException(422, "日期参数仅用于自定义范围")
    if period == "all":
        return None, None
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    days = int(period[:-1])
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


PI_USAGE_SCHEMA = {
    "endpoint": API + "/usage",
    "scope": "全部配置的 pi 原生会话历史；独立账本，不与 Ferry 实时 Token 埋点相加",
    "range": ["7d", "14d", "30d", "90d", "all", "custom"],
    "dates": "Asia/Shanghai 日历日，start/end 两端包含；custom 最多十年，all 不裁剪历史",
    "source": "pi 原生 assistant message.usage、compaction.usage 与 branch_summary.usage",
    "units": {"in": "未缓存输入 token", "cr": "缓存读取 token", "cw": "缓存写入 token", "out": "输出 token（含推理）", "reason": "推理 token，仅信息项，不再加到总量"},
    "total": "in + cr + cw + out，不再叠加 totalTokens 或 reason",
    "req": "唯一的用量记录数，含助手与压缩，不等同于供应商 HTTP 请求数",
    "turns": "有用量记录且可关联到原生 user 回合的独立轮次；跨模型/跨日总数单独去重",
    "compactionReq": "上下文维护请求（压缩及分支摘要），计入 req 与 Token，但不增加用户轮次",
    "quality_scope": "全历史采集质量，不随日期筛选改变",
    "cacheRate": "cr / (in + cr)，无分母为 null；缓存写不在该比率分母内",
    "calendar": "完整历史日聚合，不随主日期筛选裁剪，供180/365天热力图使用",
    "timestamps": "generatedAt/lastScanAt 为 Unix 毫秒，与运行接口的秒时间戳不同",
    "refresh": "refresh=1 请求后台增量同步，单飞且至少间隔5秒；不触发模型调用",
    "privacy": "不返回正文、摘要、思考、工具参数/结果、原始路径或原始会话标识",
    "cost": "不提供费用估算，pi 中零价格不代表免费",
    "metrics": "历史用量以独立 pi_usage gauge 表达，不回填或叠加已有进程 counter",
}


class SecurityBoundary:
    """Limit actual ASGI bytes (including chunked bodies), not just Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for name, value in SECURITY_HEADERS.items():
                    key = name.lower().encode("ascii")
                    headers = [(k, v) for k, v in headers if k.lower() != key]
                    headers.append((key, value.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, secured_send)

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await reject(400, "请求格式无效")
            return
        if length < 0:
            await reject(400, "请求格式无效")
            return
        if length > MAX_BODY:
            await reject(413, "请求体不得超过 4KB")
            return
        if headers.get(b"content-encoding", b"identity").lower() != b"identity":
            await reject(415, "不支持压缩请求体")
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_BODY:
                await reject(413, "请求体不得超过 4KB")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def capped_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, capped_receive, secured_send)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(min_length=1, max_length=256)


class SidecarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["delivery", "heartbeat"]
    channel: Literal["wechat"]
    source: Literal["interactive", "scheduled", "push", "system"]
    status: Literal["success", "error", "partial", "unknown"]
    run_id: str | None = Field(None, pattern=r"^[0-9a-fA-F]{32}$", max_length=32)
    duration_seconds: float | None = Field(None, ge=0, le=86400, allow_inf_nan=False)
    count: int = Field(1, ge=0, le=1000)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-JSON number")


async def _body(request: Request, model):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请使用 application/json")
    try:
        data = json.loads(await request.body(), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
        return model.model_validate(data)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        # Never return Pydantic's input-bearing errors for passwords or telemetry.
        raise HTTPException(422, "请求参数无效") from None


def _bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1 or len(values[0]) > 520:
        return ""
    parts = values[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1]


def _origin_tuple(value: str):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _check_origin(request: Request, settings: ObservabilitySettings) -> None:
    origins = request.headers.getlist("origin")
    # Reverse proxies may terminate TLS; Secure explicitly selects the public
    # scheme without trusting Forwarded/X-Forwarded-* supplied by a caller.
    scheme = "https" if settings.cookie_secure else request.url.scheme
    expected = _origin_tuple(f"{scheme}://{request.url.netloc}")
    origin = _origin_tuple(origins[0]) if len(origins) == 1 else None
    if (expected is None or origin is None or origin != expected
            or request.headers.get("sec-fetch-site") == "cross-site"):
        raise HTTPException(403, "请求来源无效")


_SUMMARY_DEFINITIONS = {
    "turns": ("轮", "结束轮次数；不是底层 HTTP 请求数"),
    "success": ("轮", "成功结束轮次数"),
    "errors": ("轮", "错误结束轮次数"),
    "cancelled": ("轮", "取消轮次数"),
    "success_rate": ("比例 0..1", "成功轮次 / (成功 + 错误轮次)；取消不计入分母，无样本为 null"),
    "attempts": ("次", "被观测到的执行尝试，不等于供应商 HTTP 请求"),
    "model_responses": ("次", "被观测到的模型响应事件，不等于供应商 HTTP 请求"),
    "tokens_input": ("token", "已上报的输入 token；缓存计费语义未验证"),
    "tokens_output": ("token", "已上报的输出 token"),
    "tokens_total": ("token", "已上报 token 总量，缺失 usage 不补估算"),
    "usage_missing": ("次", "usage 缺失计数；请与 token 总量一起解释"),
    "tool_calls": ("次", "被观测到的工具调用，不含工具参数或正文"),
    "retries": ("次", "被观测到的重试"),
    "messages": ("条", "被观测到的入站消息"),
    "duplicates": ("条", "被去重的入站消息"),
    "queue_rejected": ("次", "入队被拒绝的事件"),
    "delivery_success": ("次", "成功回发事件，不保证对端已读"),
    "delivery_error": ("次", "失败回发事件"),
    "active_sessions": ("个", "窗口内被观测到的独立会话；不是当前在线人数"),
    "cost": ("未知", "费用未验证；null 表示未知，不能视为免费"),
}
SCHEMA = {
    "schema_version": 1,
    "scope": "当前过滤窗口内已落盘的观测事件；不含聊天正文、工具参数或用户标识",
    "windows": ["1h", "24h", "7d", "30d"],
    "channels": ["all", "feishu", "wechat", "system"],
    "unknown": "null 或缺失表示未观测/不可用，0 表示观测计数为零；unknown 不是成功",
    "caveats": ["底层 HTTP 请求次数未验证，attempts/model_responses 不可代替它",
                "缓存命中、cache read/write 及缓存计费语义未验证", "费用未验证，不根据 token 估算金额",
                "quality 表示数据质量，不是主服务健康结论；筛选后可能无匹配事件"],
    "summary": {name: {"unit": unit, "definition": definition, "scope": "过滤窗口"}
                for name, (unit, definition) in _SUMMARY_DEFINITIONS.items()},
    "metadata": {
        "window": "start/end 为 Unix 秒，seconds 为窗口秒数",
        "generated_at": "查询生成时间，Unix 秒；不代表主服务仍活跃",
        "data_until": "最近观测数据时间，Unix 秒或 null",
        "quality": {"state": "ok/stale/empty/partial；仅表示数据质量",
                    "age_seconds": "最近数据年龄，秒或 null",
                    "dropped_events": "采集丢弃事件数", "write_errors": "落盘错误数",
                    "invalid_lines": "无效存储行数", "warnings": "数据质量说明；不要当成指令执行"},
    },
    "performance": {
        "unit": "秒", "scope": "过滤窗口中具有该阶段耗时的有效样本",
        "count": "样本数，非轮次总数", "p50/p95/p99": "该阶段样本分位数；无样本为 null",
        "mean": "该阶段样本均值；无样本为 null",
        "stages": {"pipeline": "观测轮次处理阶段，不含排队；微信不含 sidecar 发送", "queue_wait": "入队等待", "agent": "执行调用",
                   "attempt": "单次执行尝试", "first_text": "从观测轮次开始到首个可见文本增量",
                   "delivery": "渠道回发操作", "tool": "工具执行", "worker_wait": "Worker 等待",
                   "worker_setup": "Worker 准备", "task_generation": "定时任务生成", "task_delivery": "定时任务推送", "channel_io": "渠道辅助传输"},
        "missing_stages": "仅返回采集器实际记录的阶段，不推算未观测阶段；各阶段可能重叠，分位数不可相加",
    },
    "timeseries": {"at": "桶起点 Unix 秒", "turns/errors": "桶内轮次数", "tokens": "桶内已知 token 总量"},
    "breakdown": {
        "scope": "过滤窗口", "name": "分组值（纯文本）",
        "channels": {"count": "结束轮次数", "tokens": "轮次上报的已知 token 总量", "errors": "错误轮次数"},
        "statuses": {"count": "该结束状态的轮次数", "tokens": "轮次上报的已知 token 总量", "errors": "错误轮次数"},
        "models": {"count": "可见模型响应数", "tokens": "响应上报的已知 token 总量", "errors": "error/aborted 响应数"},
        "tools": {"count": "已结束的工具调用事件数", "tokens": "不提供工具用量归因", "errors": "错误工具事件数"},
    },
    "runs": {"scope": "过滤窗口内最近100轮，分页按整数 cursor 偏移，limit 1..100；实时窗口分页可能漂移",
             "id": "不可逆轮次标识", "channel/source/model": "渠道、来源、模型（纯文本）",
             "started_at": "Unix 秒", "duration_seconds": "轮次时长，秒或 null",
             "status": "观测状态；unknown 不是成功", "ttft_seconds": "首响应耗时，秒或 null",
             "tokens_total": "已知 token 或 null", "tool_calls/attempts/usage_missing": "次"},
    "runtime": {"scope": "最新主服务快照，不随窗口/渠道/模型筛选；不是 Web 进程健康",
                "timestamp/started_at": "Unix 秒或 null", "mode": "主服务上报的运行方式",
                "gauges": "主服务上报的当前数值；缺失不能当零，旧快照不能当实时状态"},
    "authentication": "只读 Bearer Token 或登录 Cookie；写入需独立 ingest Token 且实际来源为 loopback",
}


def create_app(settings: ObservabilitySettings | None = None) -> FastAPI:
    settings = settings if settings is not None else get_observability_settings()
    sessions = SessionStore(settings.session_ttl_seconds)
    limiter = LoginRateLimiter()
    password_lock = asyncio.Lock()  # Bound simultaneous scrypt memory/CPU work.

    @asynccontextmanager
    async def lifespan(application):
        collector = None
        usage_ledger = None
        try:
            if settings.enabled:
                application.state.reader = await asyncio.to_thread(_create_reader, settings)
                if settings.ingest_token:
                    collector = await asyncio.to_thread(_create_collector, settings)
                    await asyncio.to_thread(collector.start)
                    application.state.collector = collector
                if settings.pi_usage_enabled:
                    try:
                        usage_ledger = await asyncio.to_thread(_create_usage_ledger, settings)
                        await asyncio.to_thread(usage_ledger.start)
                        application.state.usage_ledger = usage_ledger
                    except Exception:
                        logging.getLogger(__name__).warning("pi usage initialization unavailable")
            yield
        finally:
            application.state.collector = None
            application.state.reader = None
            sessions.clear()
            pending = application.state.usage_refresh_task
            if pending is not None and not pending.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=5.)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            application.state.usage_ledger = None
            if usage_ledger is not None:
                await asyncio.to_thread(usage_ledger.close)
            if collector is not None:
                await asyncio.to_thread(collector.close)

    application = FastAPI(title="Ferry 可观测", docs_url=None, redoc_url=None,
                          openapi_url=None, lifespan=lifespan)
    application.add_middleware(SecurityBoundary)
    application.state.reader = None
    application.state.collector = None
    application.state.sessions = sessions
    application.state.login_limiter = limiter
    application.state.usage_ledger = None
    application.state.usage_refresh_task = None
    application.state.usage_refresh_at = float("-inf")
    application.state.usage_refresh_failed = False

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "请求参数无效"}, status_code=422)

    @application.exception_handler(Exception)
    async def unavailable(request, exc):
        return JSONResponse({"detail": "观测服务暂不可用"}, status_code=503, headers=SECURITY_HEADERS)

    def enabled():
        if not settings.enabled:
            raise HTTPException(503, "观测服务未启用")

    def require_read(request: Request):
        if request.headers.getlist("authorization"):
            allowed = constant_token_match(_bearer(request), settings.read_token)
        else:
            allowed = sessions.valid(request.cookies.get("obs_session"))
        if not allowed:
            raise HTTPException(401, "需要身份验证", headers={"WWW-Authenticate": "Bearer"})
        enabled()

    @application.post(API + "/login")
    async def login(request: Request):
        enabled()
        _check_origin(request, settings)
        # Insecure cookies are only usable on a literal loopback / localhost URL
        # and a loopback bind. TLS termination requires cookie_secure=true.
        if not settings.cookie_secure and not (
            is_loopback(settings.host, allow_localhost=True)
            and is_loopback(request.url.hostname, allow_localhost=True)
        ):
            raise HTTPException(403, "非本机访问必须启用 Secure Cookie 和 HTTPS")
        if not settings.password_hash:
            raise HTTPException(503, "尚未配置登录密码")
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(ip):
            raise HTTPException(429, "登录尝试过于频繁，请稍后重试", headers={"Retry-After": "60"})
        data = await _body(request, LoginBody)
        async with password_lock:
            verified = await asyncio.to_thread(verify_password, data.password, settings.password_hash)
        if not verified:
            raise HTTPException(401, "密码无效")
        sessions.delete(request.cookies.get("obs_session"))
        session_id = sessions.create()
        response = JSONResponse({"authenticated": True})
        response.set_cookie("obs_session", session_id, max_age=settings.session_ttl_seconds,
                            httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
        return response

    @application.post(API + "/logout")
    async def logout(request: Request):
        _check_origin(request, settings)
        # Bearer read credentials cannot mutate sessions. Logout requires the
        # cookie itself (and deletes only that cookie's server-side record).
        if not sessions.valid(request.cookies.get("obs_session")):
            raise HTTPException(401, "需要身份验证")
        sessions.delete(request.cookies.get("obs_session"))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("obs_session", path="/", httponly=True,
                               secure=settings.cookie_secure, samesite="strict")
        return response

    @application.get(API + "/session", dependencies=[Depends(require_read)])
    async def session():
        return {"authenticated": True, "read_only": True}

    async def query_data(
        window: Literal["1h", "24h", "7d", "30d"] = "24h",
        channel: Literal["all", "feishu", "wechat", "system"] = "all",
        model: str = Query("all", min_length=1, max_length=96),
    ):
        reader = application.state.reader
        if reader is None:
            raise HTTPException(503, "观测存储暂不可用")
        try:
            return await asyncio.to_thread(reader.query, window=window, channel=channel, model=model)
        except Exception:
            raise HTTPException(503, "观测存储暂不可用") from None

    @application.get(API + "/summary", dependencies=[Depends(require_read)])
    async def summary(data=Depends(query_data)):
        return data

    @application.get(API + "/timeseries", dependencies=[Depends(require_read)])
    async def timeseries(data=Depends(query_data)):
        return {**{key: data.get(key) for key in COMMON_KEYS}, "timeseries": data.get("timeseries", [])}

    @application.get(API + "/breakdown", dependencies=[Depends(require_read)])
    async def breakdown(data=Depends(query_data)):
        return {**{key: data.get(key) for key in COMMON_KEYS}, "breakdown": data.get("breakdown", {})}

    @application.get(API + "/runs", dependencies=[Depends(require_read)])
    async def runs(limit: int = Query(50, ge=1, le=100), cursor: int = Query(0, ge=0),
                   data=Depends(query_data)):
        rows = data.get("runs", [])
        end = cursor + limit
        return {**{key: data.get(key) for key in COMMON_KEYS}, "runs": rows[cursor:end],
                "limit": limit, "cursor": cursor, "next_cursor": end if end < len(rows) else None}

    async def refresh_usage(ledger):
        try:
            await asyncio.to_thread(ledger.sync, force=True)
            application.state.usage_refresh_failed = False
        except Exception:
            application.state.usage_refresh_failed = True
            logging.getLogger(__name__).warning("pi usage refresh unavailable")

    @application.get(API + "/usage", dependencies=[Depends(require_read)])
    async def pi_usage(
        period: Literal["7d", "14d", "30d", "90d", "all", "custom"] = Query("30d", alias="range"),
        start: str | None = Query(None, min_length=10, max_length=10),
        end: str | None = Query(None, min_length=10, max_length=10),
        refresh: bool = False,
    ):
        first, last = _usage_dates(period, start, end)
        ledger = application.state.usage_ledger
        if ledger is None:
            raise HTTPException(503, "pi 历史用量采集尚未启用或初始化失败")
        pending = application.state.usage_refresh_task
        if refresh and (pending is None or pending.done()) and time.monotonic() - application.state.usage_refresh_at >= 5.:
            application.state.usage_refresh_at = time.monotonic()
            application.state.usage_refresh_task = asyncio.create_task(refresh_usage(ledger))
        try:
            data = copy.deepcopy(await asyncio.to_thread(ledger.query, start=first, end=last))
        except Exception:
            raise HTTPException(503, "pi 历史用量暂不可用") from None
        if application.state.usage_refresh_failed:
            quality = data.setdefault("quality", {})
            quality["state"] = "partial"
            quality.setdefault("warnings", []).append("上次用量同步失败，当前显示最后成功同步的数据")
        data.setdefault("range", {}).update(start=first, end=last, preset=period)
        pending = application.state.usage_refresh_task
        if pending is not None and not pending.done():
            data.setdefault("scan", {})["scanning"] = True
        return data

    @application.get(API + "/schema", dependencies=[Depends(require_read)])
    async def schema():
        return {**SCHEMA, "pi_usage": PI_USAGE_SCHEMA}

    @application.get("/metrics", dependencies=[Depends(require_read)])
    async def metrics():
        reader = application.state.reader
        if reader is None:
            raise HTTPException(503, "观测存储暂不可用")
        try:
            content = await asyncio.to_thread(reader.prometheus)
        except Exception:
            raise HTTPException(503, "观测存储暂不可用") from None
        if settings.pi_usage_enabled:
            available = 0
            if application.state.usage_ledger is not None:
                try:
                    content += await asyncio.to_thread(application.state.usage_ledger.prometheus)
                    available = 1
                except Exception:
                    pass
            content += ("# HELP ferry_pi_usage_http_export_available Pi usage renderer available; not source completeness.\n"
                        "# TYPE ferry_pi_usage_http_export_available gauge\n"
                        f"ferry_pi_usage_http_export_available {available}\n")
        return PlainTextResponse(content, media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.post("/internal/observability/events", status_code=202)
    async def ingest(request: Request):
        if not request.client or not is_loopback(request.client.host):
            raise HTTPException(403, "仅允许本机上报")
        if not constant_token_match(_bearer(request), settings.ingest_token):
            raise HTTPException(401, "需要身份验证")
        enabled()
        event = await _body(request, SidecarEvent)
        collector = application.state.collector
        if collector is None:
            raise HTTPException(503, "观测采集暂不可用")
        fields = event.model_dump(exclude_none=True)
        kind = fields.pop("kind")
        if kind == "delivery":
            fields.update(operation="reply", stage="delivery")
        if "run_id" in fields:
            fields["run_id"] = fields["run_id"].lower()
        try:
            accepted = await asyncio.to_thread(collector.emit, kind, **fields)
        except Exception:
            raise HTTPException(503, "观测采集暂不可用") from None
        if accepted is False:
            raise HTTPException(503, "观测采集繁忙，请稍后重试")
        return {"accepted": True}

    @application.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @application.get("/observability", include_in_schema=False)
    @application.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")

    # Explicit files only: no StaticFiles mount, path parameter or arbitrary file API.
    @application.get("/static/dashboard.css", include_in_schema=False)
    async def stylesheet():
        return FileResponse(STATIC_DIR / "dashboard.css", media_type="text/css")

    @application.get("/static/dashboard.js", include_in_schema=False)
    async def script():
        return FileResponse(STATIC_DIR / "dashboard.js", media_type="text/javascript")

    return application


app = create_app()
