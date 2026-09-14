import httpx
from fastapi import APIRouter, Request, Response

from strmflow.api.routes.common import ok
from strmflow.core.errors import AppError
from strmflow.core.security import SESSION_COOKIE, credentials_match
from strmflow.schemas.api import LoginRequest

router = APIRouter(tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    settings = request.app.state.settings
    if settings.turnstile_site_key:
        if not body.turnstile_token:
            raise AppError(400, "请先完成人机验证")
        try:
            result = await request.app.state.turnstile_http.post(
                "/turnstile/v0/siteverify",
                json={
                    "secret": settings.turnstile_secret_key,
                    "response": body.turnstile_token,
                },
                timeout=10,
            )
            verified = result.json().get("success")
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(502, "人机验证服务暂时不可用，请稍后再试") from exc
        if not verified:
            raise AppError(401, "人机验证未通过，请重试")
    if not credentials_match(body.username, body.password, settings):
        raise AppError(401, "用户名或密码错误")
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.signer.create(),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="strict",
    )
    return ok({"loggedIn": True})


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return ok({"loggedOut": True})
