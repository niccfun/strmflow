from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from strmflow.core.config import Settings

SESSION_COOKIE = "strm_session"


class SessionSigner:
    def __init__(self, settings: Settings) -> None:
        self.ttl = settings.session_ttl_seconds
        self._key = (settings.session_secret or secrets.token_hex(32)).encode()

    def create(self) -> str:
        expiry = str(int(time.time()) + self.ttl)
        signature = hmac.new(self._key, expiry.encode(), hashlib.sha256).hexdigest()
        return f"{expiry}.{signature}"

    def verify(self, token: str | None) -> bool:
        try:
            expiry, signature = (token or "").split(".", 1)
            if int(expiry) < int(time.time()):
                return False
        except (ValueError, TypeError):
            return False
        expected = hmac.new(self._key, expiry.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def credentials_match(username: str, password: str, settings: Settings) -> bool:
    return hmac.compare_digest(username, settings.app_user) and hmac.compare_digest(
        password, settings.app_password
    )
