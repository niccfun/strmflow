from __future__ import annotations

from base64 import b64decode

from fastapi import Request

from strmflow.core.errors import AppError
from strmflow.core.security import SESSION_COOKIE, credentials_match


async def require_auth(request: Request) -> None:
    settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Basic "):
        try:
            username, password = b64decode(authorization[6:]).decode().split(":", 1)
            if credentials_match(username, password, settings):
                return
        except (ValueError, UnicodeError):
            pass
    if request.app.state.signer.verify(request.cookies.get(SESSION_COOKIE)):
        return
    raise AppError(401, "需要登录")


def services(request: Request):
    return request.app.state.services
