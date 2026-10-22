from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.web_chat_store import ChatError

logger = logging.getLogger(__name__)
_ID = re.compile(r"^[a-f0-9]{32}$")
_OWNER = re.compile(r"^[a-f0-9]{64}$")
_HOSTS = {"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"}
_TTL = 86400
_LABELS = {"reply": "回复已完成", "reminder": "提醒已到", "daily": "简报已就绪", "test": "通知已开启"}


def validate_subscription(value: dict) -> dict:
    endpoint = value.get("endpoint", "")
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048:
        raise ChatError("invalid_subscription", "通知订阅地址无效")
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ""
        allowed = host in _HOSTS or host.endswith(".push.apple.com") or host.endswith(".notify.windows.com")
        if (url.scheme != "https" or not allowed or url.port not in (None, 443) or url.username
                or url.password or url.fragment or not url.path or any(c.isspace() for c in endpoint)):
            raise ValueError()
        keys = value["keys"]
        decoded = {}
        for name, size in (("p256dh", 65), ("auth", 16)):
            raw = keys[name]
            if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}={0,2}", raw):
                raise ValueError()
            decoded[name] = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
            if len(decoded[name]) != size:
                raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded["p256dh"])
    except (ValueError, KeyError, TypeError):
        raise ChatError("invalid_subscription", "通知订阅格式或服务地址不支持") from None
    return {"endpoint": endpoint, "keys": {name: keys[name] for name in ("p256dh", "auth")}}


def _send(subscription: dict, payload: dict, key_path: Path, contact: str) -> int:
    import requests
    from pywebpush import WebPushException, webpush

    host = urlsplit(validate_subscription(subscription)["endpoint"]).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        return 400

    class DirectSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(method, url, **kwargs)

    with DirectSession() as session:
        session.trust_env = False
        try:
            response = webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                               vapid_private_key=str(key_path), vapid_claims={"sub": contact},
                               ttl=300, timeout=10, requests_session=session)
            return response.status_code
        except WebPushException as exc:
            return exc.response.status_code if exc.response is not None else 503


class WebPush:
    def __init__(self, root: str, contact: str, chat_store, verify_owner, *, sender=_send):
        if not (re.fullmatch(r"mailto:[^\s@]+@[^\s@]+", contact) or
                (contact.startswith("https://") and urlsplit(contact).hostname)):
            raise ValueError("WEB_PUSH_CONTACT must be a mailto or HTTPS contact")
        self.root = Path(root).absolute()
        if any(part.is_symlink() for part in (self.root, *self.root.parents)):
            raise ValueError("Unsafe push storage")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path, self.key_path = self.root / "subscriptions.json", self.root / "vapid.pem"
        self._lock = threading.RLock()
        self.contact, self.chat_store, self.verify_owner, self.sender = contact, chat_store, verify_owner, sender
        self._task = None
        self._visible = {}
        if self.key_path.exists():
            key = serialization.load_pem_private_key(self._read(self.key_path), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                raise ValueError("Invalid VAPID key")
        else:
            if self.path.exists():
                raise ValueError("VAPID key is missing; existing subscriptions cannot be reused")
            key = ec.generate_private_key(ec.SECP256R1())
            self._write(self.key_path, key.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.public_key = base64.urlsafe_b64encode(key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode()
        self.state = {"version": 1, "subscriptions": {}, "outbox": [], "seen": {}, "read": {}}
        if self.path.exists():
            data = json.loads(self._read(self.path))
            if (data.get("version") != 1 or not isinstance(data.get("subscriptions"), dict)
                    or not isinstance(data.get("outbox"), list) or not isinstance(data.get("seen"), dict)
                    or len(data["subscriptions"]) > 32 or len(data["outbox"]) > 512 or len(data["seen"]) > 4096):
                raise ValueError("Invalid push storage")
            for identity, subscription in data["subscriptions"].items():
                if not _ID.fullmatch(identity) or not _OWNER.fullmatch(subscription["owner"]):
                    raise ValueError("Invalid push owner")
                validate_subscription(subscription["subscription"])
            data.setdefault("read", {})
            if not isinstance(data["read"], dict) or len(data["read"]) > 4096:
                raise ValueError("Invalid notification read state")
            self.state = data

    @staticmethod
    def _read(path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            stat = os.fstat(file.fileno())
            if stat.st_mode & 0o077 or stat.st_size > 4 * 1024 * 1024:
                raise ValueError("Unsafe push file")
            return file.read()

    @staticmethod
    def _write(path: Path, content: bytes):
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _commit(self, state):
        self._write(self.path, json.dumps(state, ensure_ascii=False, allow_nan=False).encode())
        self.state = state

    def status(self, owner: str) -> dict:
        with self._lock:
            subscribed = [s for s in self.state["subscriptions"].values()
                          if s["owner"] == owner and s["expires_at"] > time.time()]
            return {"enabled": True, "public_key": self.public_key, "subscribed": bool(subscribed),
                    "scheduled": any(s["scheduled"] for s in subscribed)}

    def subscribe(self, owner: str, expires_at: float, subscription: dict, scheduled: bool) -> dict:
        subscription = validate_subscription(subscription)
        if not _OWNER.fullmatch(owner) or not time.time() < expires_at <= time.time() + 366 * 86400:
            raise ChatError("invalid_owner", "登录已失效", 401)
        with self._lock:
            state = copy.deepcopy(self.state)
            state["subscriptions"] = {k: s for k, s in state["subscriptions"].items() if s["expires_at"] > time.time()}
            existing = next((k for k, s in state["subscriptions"].items()
                             if s["subscription"]["endpoint"] == subscription["endpoint"]), None)
            if existing is None and len(state["subscriptions"]) >= 32:
                raise ChatError("subscription_limit", "通知设备过多，请先关闭其他设备通知", 409)
            identity = existing or uuid.uuid4().hex
            previous = state["subscriptions"].get(identity, {})
            previous_owner = previous.get("owner")
            generation = previous.get("generation") if previous_owner == owner and previous.get("subscription") == subscription else None
            state["subscriptions"][identity] = {"owner": owner, "expires_at": expires_at,
                                               "subscription": subscription, "scheduled": bool(scheduled),
                                               "generation": generation or uuid.uuid4().hex}
            state["outbox"] = [item for item in state["outbox"] if item["subscription_id"] != identity
                               or (previous_owner == owner and (scheduled or item["kind"] in {"reply", "test"}))]
            self._commit(state)
        return self.status(owner)

    def unsubscribe(self, owner: str):
        with self._lock:
            state = copy.deepcopy(self.state)
            state["subscriptions"] = {k: s for k, s in state["subscriptions"].items() if s["owner"] != owner}
            state["outbox"] = [item for item in state["outbox"] if item["owner"] != owner]
            self._visible.pop(owner, None)
            self._commit(state)

    def presence(self, owner: str, thread_id: str | None, finished_turn_id: str | None = None):
        with self._lock:
            if thread_id:
                if not _ID.fullmatch(thread_id):
                    raise ChatError("invalid_thread", "对话标识无效")
                self._visible[owner] = (thread_id, time.time() + 40)
            else:
                self._visible.pop(owner, None)
            if finished_turn_id:
                if not thread_id or not _ID.fullmatch(finished_turn_id):
                    raise ChatError("invalid_turn", "轮次标识无效")
                event_key = hashlib.sha256(f"{thread_id}:{finished_turn_id}".encode()).hexdigest()
                state = copy.deepcopy(self.state)
                state["read"] = {key: expiry for key, expiry in state["read"].items() if expiry > time.time()}
                if len(state["read"]) >= 4096:
                    del state["read"][min(state["read"], key=state["read"].get)]
                state["read"][f"{owner}:{event_key}"] = time.time() + _TTL
                state["outbox"] = [item for item in state["outbox"] if item["owner"] != owner or item["event_id"] != event_key]
                self._commit(state)

    def enqueue(self, event: dict):
        kind = event["kind"]
        thread = event.get("thread_id", "")
        if kind not in _LABELS or (kind != "test" and not _ID.fullmatch(thread)):
            raise ValueError("Invalid notification event")
        key = hashlib.sha256(f'{thread}:{event["event_id"]}'.encode()).hexdigest()
        now = time.time()
        with self._lock:
            if key in self.state["seen"]:
                return
            state = copy.deepcopy(self.state)
            targets = [(identity, sub) for identity, sub in state["subscriptions"].items()
                       if sub["expires_at"] > now and (sub["owner"] == event.get("owner")
                          if kind in {"reply", "test"} else sub["scheduled"])]
            if len(state["outbox"]) + len(targets) > 512:
                raise RuntimeError("Notification queue full")
            state["outbox"].extend({"id": uuid.uuid4().hex, "subscription_id": identity, "owner": sub["owner"],
                                    "event_id": key, "thread_id": thread, "kind": kind,
                                    "created_at": now, "attempts": 0, "next_at": now} for identity, sub in targets)
            state["seen"] = {key: stamp for key, stamp in state["seen"].items() if stamp > now - _TTL}
            if len(state["seen"]) >= 4096:
                raise RuntimeError("Notification event limit")
            state["seen"][key] = now
            self._commit(state)

    def test(self, owner: str):
        if not self.status(owner)["subscribed"]:
            raise ChatError("not_subscribed", "请先开启通知", 409)
        self.enqueue({"event_id": uuid.uuid4().hex, "kind": "test", "owner": owner})

    def _result(self, item_id: str, status: int, generation: str | None = None):
        with self._lock:
            state = copy.deepcopy(self.state)
            item = next((i for i in state["outbox"] if i["id"] == item_id), None)
            if item is None:
                return
            current = state["subscriptions"].get(item["subscription_id"])
            if generation is not None and (not current or current.get("generation") != generation):
                return
            if status in {404, 410}:
                state["subscriptions"].pop(item["subscription_id"], None)
                state["outbox"] = [i for i in state["outbox"] if i["subscription_id"] != item["subscription_id"]]
            elif status in {408, 429, 503} or status >= 500:
                item["attempts"] += 1
                if item["attempts"] < 8 and item["created_at"] > time.time() - _TTL:
                    item["next_at"] = time.time() + min(3600, 15 * 2 ** item["attempts"])
                else:
                    state["outbox"].remove(item)
            else:
                state["outbox"].remove(item)
            self._commit(state)

    async def tick(self):
        for event in await asyncio.to_thread(self.chat_store.pending_notifications):
            try:
                await asyncio.to_thread(self.enqueue, event)
            except RuntimeError:
                break
            await asyncio.to_thread(self.chat_store.ack_notification, event["thread_id"], event["event_id"])
        with self._lock:
            pending = [copy.deepcopy(i) for i in self.state["outbox"] if i["next_at"] <= time.time()]
        attempted = 0
        for item in pending:
            with self._lock:
                sub = copy.deepcopy(self.state["subscriptions"].get(item["subscription_id"]))
                visible = self._visible.get(item["owner"])
                read_until = self.state["read"].get(f'{item["owner"]}:{item["event_id"]}', 0)
            if (not sub or sub["owner"] != item["owner"] or sub["expires_at"] <= time.time()
                    or item["created_at"] <= time.time() - _TTL or read_until > time.time()):
                await asyncio.to_thread(self._result, item["id"], 204)
                continue
            if item["kind"] != "test":
                try:
                    await asyncio.to_thread(self.chat_store.get_thread, item["thread_id"])
                except ChatError as exc:
                    if exc.status == 404:
                        await asyncio.to_thread(self._result, item["id"], 204)
                        continue
                    raise
                if item["kind"] == "reply" and visible and visible[0] == item["thread_id"] and visible[1] > time.time():
                    continue
            if attempted >= 16:
                break
            attempted += 1
            valid = await self.verify_owner(item["owner"])
            if valid is False:
                await asyncio.to_thread(self.unsubscribe, item["owner"])
                continue
            if valid is None:
                await asyncio.to_thread(self._result, item["id"], 503)
                continue
            with self._lock:
                current = self.state["subscriptions"].get(item["subscription_id"])
                if (not any(i["id"] == item["id"] for i in self.state["outbox"])
                        or not current or current.get("generation") != sub.get("generation")):
                    continue
            payload = {"title": "Ferry", "body": _LABELS[item["kind"]], "tag": item["event_id"],
                       "thread_id": item["thread_id"]}
            try:
                status = await asyncio.to_thread(self.sender, sub["subscription"], payload, self.key_path, self.contact)
            except Exception:
                status = 503
            await asyncio.to_thread(self._result, item["id"], status, sub.get("generation"))

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("notification dispatch deferred", extra={"error_code": type(exc).__name__})
            await asyncio.sleep(2)

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
