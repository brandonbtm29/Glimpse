"""Passwords, login cookies, feed tokens and login rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..config import AppConfig, AudienceCfg

SESSION_DAYS = 30
_hasher = PasswordHasher()


def verify_password(audience: AudienceCfg, password: str) -> bool:
    if not audience.password_hash:
        return True
    try:
        return _hasher.verify(audience.password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _fingerprint(audience: AudienceCfg) -> str:
    """Changes whenever the slug or password changes, which logs everyone out."""
    raw = f"{audience.slug}|{audience.password_hash or ''}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def cookie_name(audience_id: str) -> str:
    return "glimpse_" + hashlib.sha256(audience_id.encode()).hexdigest()[:10]


def _serializer(cfg: AppConfig) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.server.secret_key, salt="glimpse-login")


def make_session(cfg: AppConfig, audience_id: str) -> str:
    aud = cfg.audiences[audience_id]
    return _serializer(cfg).dumps({"a": audience_id, "f": _fingerprint(aud)})


def session_valid(cfg: AppConfig, audience_id: str, token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _serializer(cfg).loads(token, max_age=SESSION_DAYS * 86400)
    except BadSignature:
        return False
    aud = cfg.audiences[audience_id]
    return (
        isinstance(data, dict)
        and data.get("a") == audience_id
        and hmac.compare_digest(str(data.get("f", "")), _fingerprint(aud))
    )


def feed_token(cfg: AppConfig, audience_id: str) -> str:
    """Secret token for the subscribe-able ICS feed (calendar apps can't log in)."""
    aud = cfg.audiences[audience_id]
    msg = f"feed|{audience_id}|{_fingerprint(aud)}".encode()
    digest = hmac.new(cfg.server.secret_key.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:18]).decode().rstrip("=")


class LoginLimiter:
    """5 failed attempts per IP per minute; 20 per audience per 15 minutes."""

    def __init__(
        self, per_ip: int = 5, ip_window: float = 60, per_audience: int = 20, audience_window: float = 900
    ):
        self.per_ip, self.ip_window = per_ip, ip_window
        self.per_audience, self.audience_window = per_audience, audience_window
        self._ip: dict[str, deque[float]] = defaultdict(deque)
        self._aud: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _trim(q: deque[float], window: float, now: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    def blocked(self, audience_id: str, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            qi, qa = self._ip[ip], self._aud[audience_id]
            self._trim(qi, self.ip_window, now)
            self._trim(qa, self.audience_window, now)
            return len(qi) >= self.per_ip or len(qa) >= self.per_audience

    def failed(self, audience_id: str, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._ip[ip].append(now)
            self._aud[audience_id].append(now)
