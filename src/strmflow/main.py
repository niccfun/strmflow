from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from strmflow import __version__
from strmflow.api.deps import require_auth
from strmflow.api.router import router
from strmflow.container import build_container
from strmflow.core.config import Settings, get_settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore, request_category, status_level
from strmflow.core.security import SESSION_COOKIE, SessionSigner
from strmflow.infrastructure.database import Database

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
        "connect-src 'self'; frame-src https://challenges.cloudflare.com; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    runtime_logs = RuntimeLogStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.validate_runtime()
        database = Database(settings)
        await database.initialize()
        try:
            async with (
                httpx.AsyncClient(
                    base_url=settings.openlist_url.rstrip("/") + "/"
                ) as openlist_http,
                httpx.AsyncClient(
                    base_url=(settings.emby_url or "http://127.0.0.1").rstrip("/") + "/"
                ) as emby_http,
                httpx.AsyncClient(base_url="https://challenges.cloudflare.com") as turnstile_http,
            ):
                app.state.settings = settings
                app.state.signer = SessionSigner(settings)
                app.state.services = build_container(
                    settings, database, openlist_http, emby_http, runtime_logs
                )
                app.state.turnstile_http = turnstile_http
                await app.state.services.path_config.initialize()
                await app.state.services.transfers.initialize()
                await app.state.services.legacy_importer.run_once()
                await app.state.services.emby302.initialize()
                await app.state.services.emby302.start_configured()
                await app.state.services.bdpan.initialize()
                runtime_logs.add(
                    category="system",
                    level="success",
                    message="StrmFlow 服务启动完成",
                )
                try:
                    yield
                finally:
                    await app.state.services.bdpan.close()
                    await app.state.services.emby302.close()
                    await app.state.services.transfers.close()
        finally:
            await database.close()

    app = FastAPI(
        title="StrmFlow API",
        version=__version__,
        description="OpenList STRM 追更、Emby 发布及可插拔转存服务",
        lifespan=lifespan,
    )
    app.state.runtime_logs = runtime_logs

    @app.middleware("http")
    async def runtime_access_log(request: Request, call_next):
        path = request.url.path
        if path.rstrip("/") == "/api/logs":
            return await call_next(request)

        started_at = perf_counter()
        method = request.method.upper()
        client = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "")[:240]
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((perf_counter() - started_at) * 1_000, 2)
            runtime_logs.add(
                category=request_category(path),
                level="error",
                message=f"{method} {path}",
                method=method,
                path=path,
                statusCode=500,
                durationMs=duration_ms,
                client=client,
                protocol=f"HTTP/{request.scope.get('http_version', '1.1')}",
                userAgent=user_agent,
            )
            raise

        duration_ms = round((perf_counter() - started_at) * 1_000, 2)
        status_code = response.status_code
        runtime_logs.add(
            category=request_category(path),
            level=status_level(status_code),
            message=f"{method} {path}",
            method=method,
            path=path,
            statusCode=status_code,
            durationMs=duration_ms,
            client=client,
            protocol=f"HTTP/{request.scope.get('http_version', '1.1')}",
            responseSize=response.headers.get("content-length", ""),
            redirectTo=response.headers.get("location", ""),
            userAgent=user_agent,
        )
        return response

    app.mount(
        "/static",
        StaticFiles(directory=settings.templates_dir.parent / "static"),
        name="static",
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        runtime_logs.add(
            category=request_category(request.url.path),
            level="error" if exc.status_code >= 500 else "warning",
            message=exc.message,
            eventType="application",
            statusCode=exc.status_code,
            path=request.url.path,
        )
        payload = {"ok": False, "error": exc.message}
        if exc.details is not None:
            payload["details"] = exc.details
        return JSONResponse(
            payload, status_code=exc.status_code, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        runtime_logs.add(
            category=request_category(request.url.path),
            level="warning",
            message="请求参数不合法",
            eventType="validation",
            statusCode=422,
            path=request.url.path,
            errorCount=len(exc.errors()),
        )
        details = [
            {key: value for key, value in error.items() if key not in {"input", "url"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            {
                "ok": False,
                "error": "请求参数不合法",
                "details": details,
            },
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        message = str(exc) if settings.debug else "Internal Server Error"
        runtime_logs.add(
            category=request_category(request.url.path),
            level="error",
            message=message,
            eventType="exception",
            statusCode=500,
            path=request.url.path,
            errorType=type(exc).__name__,
        )
        return JSONResponse(
            {"ok": False, "error": message},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request):
        if request.app.state.signer.verify(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=302)
        html = (settings.templates_dir / "login.html").read_text(encoding="utf-8")
        if settings.turnstile_site_key:
            site_key = re.sub(r"[^0-9A-Za-z_-]", "", settings.turnstile_site_key)
            html = html.replace(
                "__TURNSTILE_SCRIPT__",
                '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>',
            ).replace(
                "__TURNSTILE_WIDGET__",
                f'<div class="field turnstile-field"><div class="cf-turnstile" data-sitekey="{site_key}" data-theme="auto" data-language="zh-cn"></div></div>',
            )
        else:
            html = html.replace("__TURNSTILE_SCRIPT__", "").replace("__TURNSTILE_WIDGET__", "")
        return HTMLResponse(html, headers=SECURITY_HEADERS)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        try:
            await require_auth(request)
        except AppError:
            return RedirectResponse("/login", status_code=302)
        html = (settings.templates_dir / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers=SECURITY_HEADERS)

    app.include_router(router)
    return app


app = create_app()
