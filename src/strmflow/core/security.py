from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path

from strmflow.core.config import Settings

SESSION_COOKIE = "strm_session"


def resolve_session_key(settings: Settings) -> bytes:
    """Load a stable application key, creating it with owner-only permissions."""
    if settings.session_secret:
        return settings.session_secret.encode()

    path = settings.resolved_session_secret_path
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(generated)
            stream.write("\n")
    try:
        os.chmod(path, 0o600)
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("会话签名密钥读取失败") from exc
    if len(value) < 32 or len(value) > 512:
        raise RuntimeError("会话签名密钥文件格式不正确")
    return value.encode()


class LoginAttemptLimiter:
    """Small in-memory sliding window to slow password guessing."""

    def __init__(
        self,
        *,
        attempts: int = 8,
        window_seconds: int = 300,
        max_identities: int = 10_000,
    ) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_identities = max(1, max_identities)
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def retry_after(self, identity: str) -> int:
        now = time.monotonic()
        failures = self._failures.get(identity)
        if failures is None:
            return 0
        while failures and now - failures[0] >= self.window_seconds:
            failures.popleft()
        if len(failures) < self.attempts:
            if not failures:
                self._failures.pop(identity, None)
            else:
                self._failures.move_to_end(identity)
            return 0
        self._failures.move_to_end(identity)
        return max(1, int(self.window_seconds - (now - failures[0])))

    def failed(self, identity: str) -> None:
        self.retry_after(identity)
        failures = self._failures.setdefault(identity, deque())
        failures.append(time.monotonic())
        self._failures.move_to_end(identity)
        while len(self._failures) > self.max_identities:
            self._failures.popitem(last=False)

    def succeeded(self, identity: str) -> None:
        self._failures.pop(identity, None)


class SessionSigner:
    def __init__(self, settings: Settings) -> None:
        self.ttl = settings.session_ttl_seconds
        self._key = resolve_session_key(settings)

    def create(self) -> str:
        expiry = str(int(time.time()) + self.ttl)
        nonce = secrets.token_urlsafe(18)
        payload = f"{expiry}.{nonce}"
        signature = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, token: str | None) -> bool:
        try:
            expiry, nonce, signature = (token or "").split(".", 2)
            if int(expiry) < int(time.time()):
                return False
        except (ValueError, TypeError):
            return False
        if not nonce or len(nonce) > 128 or len(signature) != 64:
            return False
        expected = hmac.new(self._key, f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def credentials_match(username: str, password: str, settings: Settings) -> bool:
    return hmac.compare_digest(username, settings.app_user) and hmac.compare_digest(
        password, settings.app_password
    )
