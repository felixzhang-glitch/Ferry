"""Bounded, process-local authentication primitives; nothing is persisted."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

_N, _R, _P = 32768, 8, 1
_HASH_RE = re.compile(r"scrypt\$32768\$8\$1\$([0-9a-f]{32})\$([0-9a-f]{64})", re.ASCII)
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
        dklen=32, maxmem=64 * 1024 * 1024,
    )


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise ValueError("Password must contain 1 to 256 characters")
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    # Only our fixed-cost format is accepted. Never pass untrusted work factors,
    # salt lengths or output lengths to OpenSSL, even from a configuration file.
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        return False
    if not isinstance(encoded, str) or len(encoded) > 128:
        return False
    match = _HASH_RE.fullmatch(encoded)
    if match is None:
        return False
    try:
        actual = _derive(password, bytes.fromhex(match[1]))
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, bytes.fromhex(match[2]))


def constant_token_match(candidate: str, expected: str) -> bool:
    if not expected or not candidate or len(candidate) > 512:
        return False
    try:
        # Fixed-size comparisons also avoid revealing a configured token's length.
        return hmac.compare_digest(
            hashlib.sha256(candidate.encode("utf-8")).digest(),
            hashlib.sha256(expected.encode("utf-8")).digest(),
        )
    except UnicodeError:
        return False


def is_loopback(host: str | None, *, allow_localhost: bool = False) -> bool:
    if not host:
        return False
    if allow_localhost and host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


class SessionStore:
    def __init__(self, ttl_seconds: int = 86400, max_sessions: int = 256,
                 clock: Callable[[], float] = time.monotonic):
        if not 1 <= ttl_seconds <= 86400 or max_sessions < 1:
            raise ValueError("Invalid session bounds")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._sessions: OrderedDict[bytes, float] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(session_id: str | None) -> bytes | None:
        if not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id):
            return None
        return hashlib.sha256(session_id.encode("ascii")).digest()

    def _expire(self, now: float) -> None:
        # Fixed TTL and insertion ordering make the oldest entry expire first.
        while self._sessions and next(iter(self._sessions.values())) <= now:
            self._sessions.popitem(last=False)

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._expire(now)
            while len(self._sessions) >= self.max_sessions:
                self._sessions.popitem(last=False)
            self._sessions[self._key(session_id)] = now + self.ttl_seconds
        return session_id

    def valid(self, session_id: str | None) -> bool:
        key = self._key(session_id)
        with self._lock:
            self._expire(self._clock())
            return key is not None and key in self._sessions

    def delete(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(self._key(session_id), None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class LoginRateLimiter:
    """Sliding window limits; a full IP map rejects new IPs rather than evicting."""

    def __init__(self, per_ip: int = 5, global_limit: int = 20,
                 window_seconds: int = 60, max_ips: int = 1024,
                 clock: Callable[[], float] = time.monotonic):
        if min(per_ip, global_limit, window_seconds, max_ips) < 1:
            raise ValueError("Invalid rate-limit bounds")
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window_seconds = window_seconds
        self.max_ips = max_ips
        self._clock = clock
        self._ips: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        with self._lock:
            cutoff = self._clock() - self.window_seconds
            for key, attempts in list(self._ips.items()):
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if not attempts:
                    del self._ips[key]
            while self._global and self._global[0] <= cutoff:
                self._global.popleft()
            if len(self._global) >= self.global_limit:
                return False
            attempts = self._ips.get(ip)
            if attempts is None:
                if len(self._ips) >= self.max_ips:
                    return False
                attempts = self._ips[ip] = deque()
            if len(attempts) >= self.per_ip:
                return False
            now = self._clock()
            attempts.append(now)
            self._global.append(now)
            return True
