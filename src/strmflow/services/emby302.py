from __future__ import annotations

import asyncio
import re
import socket
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import httpx
import uvicorn
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore, status_level
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.services.openlist import OpenListClient
from strmflow.utils.paths import normalize_virtual_path

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
BODYLESS_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
VIDEO_PATH = re.compile(r"/(?:videos)/([^/]+)/(?:stream|original)(?:\.[^/]*)?$", re.IGNORECASE)
ITEM_PATH = re.compile(r"/(?:videos|items)/([^/]+)", re.IGNORECASE)
PLAYBACK_INFO_PATH = re.compile(r"/items/[^/]+/playbackinfo$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Emby302Config:
    enabled: bool
    emby_url: str
    openlist_url: str
    host: str
    port: int
    cache_ttl: int
    cache_max: int
    body_buffer_max: int
    timeout_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "embyUrl": self.emby_url,
            "openlistUrl": self.openlist_url,
            "host": self.host,
            "port": self.port,
            "cacheTtl": self.cache_ttl,
            "cacheMax": self.cache_max,
            "bodyBufferMax": self.body_buffer_max,
            "timeoutMs": self.timeout_ms,
        }


class EmbeddedUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


class Emby302Gateway:
    def __init__(
        self,
        settings: Settings,
        emby_http: httpx.AsyncClient,
        openlist: OpenListClient,
        repository: RuntimeSettingsRepository,
        runtime_logs: RuntimeLogStore,
    ) -> None:
        self.settings = settings
        self.emby_http = emby_http
        self.openlist = openlist
        self.repository = repository
        self.runtime_logs = runtime_logs
        self.config = self._default_config()
        self._lock = asyncio.Lock()
        self._server: EmbeddedUvicornServer | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._booted_at = datetime.now(UTC)
        self._started_at: datetime | None = None
        self._last_request_at: datetime | None = None
        self._last_redirect_at: datetime | None = None
        self._last_error = ""
        self._total_requests = 0
        self._redirects = 0
        self._cache_hits = 0
        self._proxy_requests = 0
        self._errors = 0
        self._recent_redirects: deque[dict[str, Any]] = deque(maxlen=30)

    async def initialize(self) -> None:
        stored = await self.repository.load_emby302()
        if stored:
            try:
                self.config = self._parse_config(stored)
            except (TypeError, ValueError):
                self.runtime_logs.add(
                    category="gateway302",
                    level="warning",
                    message="已忽略无效的 302 网关持久化配置",
                )

    async def start_configured(self) -> None:
        if not self.config.enabled:
            self.runtime_logs.add(
                category="gateway302",
                level="info",
                message="302 网关当前未启用",
                listenAddress=f"{self.config.host}:{self.config.port}",
            )
            return
        try:
            await self._start_server()
        except Exception as exc:  # noqa: BLE001 - keep the management app available
            self._last_error = str(exc)
            self._errors += 1
            self.runtime_logs.add(
                category="gateway302",
                level="error",
                message=f"302 网关启动失败：{exc}",
            )

    async def close(self) -> None:
        async with self._lock:
            await self._stop_server()

    async def update(self, value: dict[str, Any]) -> dict[str, Any]:
        new_config = self._parse_config(value)
        self._validate_runtime_config(new_config)
        async with self._lock:
            old_config = self.config
            await self._stop_server()
            self.config = new_config
            self._cache.clear()
            try:
                if new_config.enabled:
                    await self._start_server()
                await self.repository.save_emby302(new_config.as_dict())
            except Exception as exc:
                await self._stop_server()
                self.config = old_config
                if old_config.enabled:
                    try:
                        await self._start_server()
                    except Exception as restore_error:  # noqa: BLE001 - preserve original failure
                        self._last_error = str(restore_error)
                raise AppError(409, f"应用 302 网关配置失败：{exc}") from exc

        self.runtime_logs.add(
            category="gateway302",
            level="success",
            message="302 网关配置已更新",
            enabled=new_config.enabled,
            listenAddress=f"{new_config.host}:{new_config.port}",
        )
        return self.snapshot()

    def clear_cache(self) -> int:
        count = len(self._cache)
        self._cache.clear()
        self.runtime_logs.add(
            category="gateway302",
            level="success",
            message=f"302 网关缓存已清空，共 {count} 项",
        )
        return count

    def snapshot(self) -> dict[str, Any]:
        self._cleanup_cache()
        running = bool(
            self._server
            and self._server.started
            and self._server_task
            and not self._server_task.done()
        )
        hit_rate = round(self._cache_hits / self._redirects * 100, 1) if self._redirects else 0
        now = datetime.now(UTC)
        active_since = self._started_at or self._booted_at
        return {
            "config": self.config.as_dict(),
            "stats": {
                "running": running,
                "totalRequests": self._total_requests,
                "redirects": self._redirects,
                "cacheHits": self._cache_hits,
                "cacheHitRate": hit_rate,
                "proxyRequests": self._proxy_requests,
                "errors": self._errors,
                "cacheEntries": len(self._cache),
                "uptimeSeconds": max(0, int((now - active_since).total_seconds()))
                if running
                else 0,
                "startedAt": self._iso(self._started_at),
                "lastRequestAt": self._iso(self._last_request_at),
                "lastRedirectAt": self._iso(self._last_redirect_at),
                "lastError": self._last_error,
                "listenAddress": f"{self.config.host}:{self.config.port}",
            },
            "recentRedirects": list(self._recent_redirects),
            "dependencies": {
                "embyConfigured": bool(self.config.emby_url and self.settings.emby_api_key),
                "openListConfigured": bool(
                    self.config.openlist_url and self.settings.openlist_token
                ),
            },
        }

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1011})
            return
        request = Request(scope, receive)
        started = perf_counter()
        self._total_requests += 1
        self._last_request_at = datetime.now(UTC)
        action = "proxy"
        try:
            response, action = await self._handle_http(request)
        except AppError as exc:
            if exc.status_code >= 500:
                self._last_error = exc.message
                self.runtime_logs.add(
                    category="gateway302",
                    level="error",
                    message=f"302 网关上游错误：{exc.message[:300]}",
                    eventType="upstream-error",
                    statusCode=exc.status_code,
                    path=request.url.path,
                )
            response = PlainTextResponse(exc.message, status_code=exc.status_code)
            action = "upstream-error"
        except Exception as exc:  # noqa: BLE001 - gateway converts failures to HTTP 500
            self._errors += 1
            self._last_error = str(exc)
            self.runtime_logs.add(
                category="gateway302",
                level="error",
                message=f"302 网关请求异常：{str(exc)[:300]}",
                eventType="exception",
                statusCode=500,
                path=request.url.path,
                errorType=type(exc).__name__,
            )
            response = PlainTextResponse("Emby 302 Gateway Error", status_code=500)
            action = "error"

        status_code = response.status_code
        duration_ms = round((perf_counter() - started) * 1_000, 2)
        if status_code >= 500 and action != "error":
            self._errors += 1
        self.runtime_logs.add(
            category="gateway302",
            level=status_level(status_code),
            message=f"{request.method} {request.url.path}",
            method=request.method,
            path=request.url.path,
            statusCode=status_code,
            durationMs=duration_ms,
            client=request.client.host if request.client else "unknown",
            protocol=f"HTTP/{scope.get('http_version', '1.1')}",
            redirectTo=response.headers.get("location", ""),
            gatewayAction=action,
            userAgent=request.headers.get("user-agent", "")[:240],
        )
        await response(scope, receive, send)

    async def _handle_http(self, request: Request) -> tuple[Response, str]:
        path = request.url.path
        lower_path = path.casefold()
        if path == "/__strmflow302/health":
            return JSONResponse({"ok": True, "stats": self.snapshot()["stats"]}), "health"
        if lower_path.endswith("/basehtmlplayer.js"):
            return await self._handle_base_html_player(request), "patched-player"
        if lower_path.endswith("/system/info"):
            return await self._handle_system_info(request), "patched-system-info"
        if PLAYBACK_INFO_PATH.search(lower_path):
            return await self._handle_playback_info(request), "patched-playback-info"
        if VIDEO_PATH.search(lower_path):
            return await self._handle_video_stream(request)
        return await self._proxy(request), "proxy"

    async def _handle_video_stream(self, request: Request) -> tuple[Response, str]:
        if request.method != "GET":
            return await self._proxy(request), "proxy"
        item_id = self._parse_item_id(request.url.path)
        if not item_id:
            return PlainTextResponse("Bad Request", status_code=400), "invalid-stream"

        media_source_id = request.query_params.get("MediaSourceId")
        user_agent = request.headers.get("user-agent", "")
        cache_key = f"{item_id}:{media_source_id or 'default'}:{user_agent}"
        cached = self._get_cache(cache_key)
        if cached:
            self._cache_hits += 1
            return self._redirect(cached, item_id, "", True), "cache-hit"

        media_source = await self._get_emby_media_source(request.url.path, item_id, media_source_id)
        if not media_source or not media_source.get("Path"):
            return await self._proxy(request), "media-source-missing"
        openlist_path = self._extract_openlist_path(media_source)
        if not openlist_path:
            return await self._proxy(request), "non-openlist-source"

        info = await self.openlist.get_file_info(
            openlist_path,
            base_url=self.config.openlist_url,
            timeout=self.config.timeout_ms / 1_000,
            user_agent=user_agent,
        )
        raw_url = str(info.get("raw_url") or info.get("rawUrl") or info.get("url") or "")
        if not raw_url:
            return PlainTextResponse("OpenList API Error", status_code=502), "openlist-error"
        self._set_cache(cache_key, raw_url)
        return self._redirect(raw_url, item_id, openlist_path, False), "redirect"

    def _redirect(
        self, raw_url: str, item_id: str, openlist_path: str, cache_hit: bool
    ) -> RedirectResponse:
        self._redirects += 1
        self._last_redirect_at = datetime.now(UTC)
        self._recent_redirects.appendleft(
            {
                "time": self._last_redirect_at.isoformat(),
                "itemId": item_id,
                "path": openlist_path,
                "cacheHit": cache_hit,
                "targetHost": urlsplit(raw_url).netloc,
            }
        )
        return RedirectResponse(self._redirect_url(raw_url), status_code=302)

    @staticmethod
    def _redirect_url(raw_url: str) -> str:
        """Percent-encode Unicode returned by OpenList before putting it in Location."""
        parsed = urlsplit(raw_url)
        try:
            netloc = parsed.netloc.encode("ascii").decode("ascii")
        except UnicodeEncodeError:
            netloc = parsed.netloc.encode("idna").decode("ascii")
        path = quote(parsed.path, safe="/%:@-._~!$&'()*+,;=")
        query = quote(parsed.query, safe="=&/?%:+,;@-._~!$'()*[]")
        fragment = quote(parsed.fragment, safe="/%?=&:+,;@-._~!$'()*[]")
        return urlunsplit((parsed.scheme, netloc, path, query, fragment))

    async def _handle_base_html_player(self, request: Request) -> Response:
        upstream = await self._proxy_buffered(request)
        if not upstream.is_success:
            return self._buffered_response(upstream)
        modified = re.sub(
            r'mediaSource\.IsRemote\s*&&\s*"DirectPlay"\s*===\s*playMethod\s*\?\s*null\s*:\s*"anonymous"',
            "null",
            upstream.text,
        )
        return Response(
            modified,
            status_code=upstream.status_code,
            media_type="application/javascript",
            headers=self._response_headers(upstream, strip_content=True),
        )

    async def _handle_system_info(self, request: Request) -> Response:
        upstream = await self._proxy_buffered(request)
        if not upstream.is_success:
            return self._buffered_response(upstream)
        try:
            body = upstream.json()
        except ValueError:
            return self._buffered_response(upstream)
        if isinstance(body, dict):
            body["WebSocketPortNumber"] = self.config.port
            body["HttpServerPortNumber"] = self.config.port
        return JSONResponse(
            body,
            status_code=upstream.status_code,
            headers=self._response_headers(upstream, strip_content=True),
        )

    async def _handle_playback_info(self, request: Request) -> Response:
        upstream = await self._proxy_buffered(request)
        if not upstream.is_success:
            return self._buffered_response(upstream)
        try:
            body = upstream.json()
        except ValueError:
            return self._buffered_response(upstream)
        if not isinstance(body, dict):
            return self._buffered_response(upstream)

        item_id = self._parse_item_id(request.url.path)
        stream_path = self._direct_stream_path(request.url.path, item_id) if item_id else ""
        sources = body.get("MediaSources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict) or not self._is_strm_media_source(source):
                    continue
                if not self._extract_openlist_path(source):
                    continue
                source["SupportsDirectPlay"] = True
                source["SupportsDirectStream"] = True
                source["SupportsTranscoding"] = False
                source.pop("TranscodingUrl", None)
                source.pop("TranscodingSubProtocol", None)
                source.pop("TranscodingContainer", None)
                if stream_path and source.get("Id"):
                    query = list(parse_qsl(request.url.query, keep_blank_values=True))
                    query = [
                        (key, value)
                        for key, value in query
                        if key not in {"MediaSourceId", "Static"}
                    ]
                    query.extend([("MediaSourceId", str(source["Id"])), ("Static", "true")])
                    source["DirectStreamUrl"] = f"{stream_path}?{urlencode(query)}"
        return JSONResponse(
            body,
            status_code=upstream.status_code,
            headers=self._response_headers(upstream, strip_content=True),
        )

    async def _get_emby_media_source(
        self, request_path: str, item_id: str, media_source_id: str | None
    ) -> dict[str, Any] | None:
        prefix = self._emby_prefix(request_path)
        url = self._upstream_url(f"{prefix}/Items", "")
        try:
            response = await self.emby_http.get(
                url,
                params={
                    "Ids": item_id,
                    "Fields": "Path,MediaSources",
                    "api_key": self.settings.emby_api_key,
                },
                timeout=self.config.timeout_ms / 1_000,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(502, f"Emby 媒体源查询失败：{exc}") from exc
        items = payload.get("Items") if isinstance(payload, dict) else None
        item = items[0] if isinstance(items, list) and items else None
        sources = item.get("MediaSources") if isinstance(item, dict) else None
        if not isinstance(sources, list) or not sources:
            return None
        if not media_source_id:
            return sources[0] if isinstance(sources[0], dict) else None
        return next(
            (
                source
                for source in sources
                if isinstance(source, dict) and source.get("Id") == media_source_id
            ),
            None,
        )

    async def _proxy(self, request: Request) -> StreamingResponse:
        self._proxy_requests += 1
        url = self._upstream_url(request.url.path, request.url.query)
        headers = self._request_headers(request)
        content: bytes | AsyncIterator[bytes] | None = None
        if request.method not in BODYLESS_METHODS:
            if self._should_buffer_body(request):
                content = await self._read_body(request)
                headers["content-length"] = str(len(content))
            else:
                content = request.stream()
        upstream_request = self.emby_http.build_request(
            request.method,
            url,
            headers=headers,
            content=content,
            timeout=self.config.timeout_ms / 1_000,
        )
        try:
            upstream = await self.emby_http.send(upstream_request, stream=True)
        except httpx.TimeoutException as exc:
            raise AppError(504, "Emby 上游请求超时") from exc
        except httpx.HTTPError as exc:
            raise AppError(502, "无法连接 Emby 上游服务") from exc
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=self._response_headers(upstream),
            background=BackgroundTask(upstream.aclose),
        )

    async def _proxy_buffered(self, request: Request) -> httpx.Response:
        self._proxy_requests += 1
        url = self._upstream_url(request.url.path, request.url.query)
        headers = self._request_headers(request)
        body = b""
        if request.method not in BODYLESS_METHODS:
            body = await self._read_body(request)
            headers["content-length"] = str(len(body))
        try:
            return await self.emby_http.request(
                request.method,
                url,
                headers=headers,
                content=body,
                timeout=self.config.timeout_ms / 1_000,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise AppError(504, "Emby 上游请求超时") from exc
        except httpx.HTTPError as exc:
            raise AppError(502, "无法连接 Emby 上游服务") from exc

    async def _read_body(self, request: Request) -> bytes:
        body = await request.body()
        if len(body) > self.config.body_buffer_max:
            raise AppError(413, f"请求体超过 {self.config.body_buffer_max} 字节")
        return body

    def _should_buffer_body(self, request: Request) -> bool:
        content_type = request.headers.get("content-type", "").casefold()
        if "application/json" in content_type or "+json" in content_type:
            return True
        if request.query_params.get("reqformat", "").casefold() == "json":
            return not any(
                marker in content_type for marker in ("multipart/", "image/", "video/", "audio/")
            )
        lower_path = request.url.path.casefold()
        return lower_path.endswith(("/ui/command", "/environment/directorycontents"))

    def _request_headers(self, request: Request) -> dict[str, str]:
        headers = {
            name: self._header_value(name, value)
            for name, value in request.headers.items()
            if name.casefold() not in HOP_BY_HOP_HEADERS | {"host", "content-length"}
        }
        headers["accept-encoding"] = "identity"
        headers["x-forwarded-host"] = request.headers.get("host", "")
        headers["x-forwarded-proto"] = request.url.scheme
        return headers

    def _response_headers(
        self, upstream: httpx.Response, *, strip_content: bool = False
    ) -> dict[str, str]:
        blocked = set(HOP_BY_HOP_HEADERS)
        if strip_content:
            blocked.update({"content-length", "content-encoding", "content-type"})
        return {
            name: self._header_value(name, value)
            for name, value in upstream.headers.items()
            if name.casefold() not in blocked
        }

    @staticmethod
    def _header_value(name: str, value: str) -> str:
        """Keep proxied response headers encodable by ASGI's latin-1 wire format."""
        try:
            value.encode("latin-1")
            return value
        except UnicodeEncodeError:
            # RFC 5987 headers can carry UTF-8, but many clients and ASGI servers
            # still require latin-1 here. Preserve the header while replacing only
            # the invalid octets instead of failing the entire playback request.
            return value.encode("latin-1", "replace").decode("latin-1")

    def _buffered_response(self, upstream: httpx.Response) -> Response:
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            headers=self._response_headers(upstream, strip_content=True),
            media_type=upstream.headers.get("content-type"),
        )

    def _upstream_url(self, path: str, query: str) -> str:
        base = urlsplit(self.config.emby_url)
        base_path = base.path.rstrip("/")
        normalized_path = "/" + path.lstrip("/")
        if base_path and not (
            normalized_path.casefold() == base_path.casefold()
            or normalized_path.casefold().startswith(base_path.casefold() + "/")
        ):
            normalized_path = base_path + normalized_path
        return urlunsplit((base.scheme, base.netloc, normalized_path, query, ""))

    def _extract_openlist_path(self, source: dict[str, Any]) -> str | None:
        source_path = str(source.get("Path") or "").strip()
        if not source_path:
            return None
        if source_path.casefold().startswith(("http://", "https://")):
            try:
                decoded = unquote(urlsplit(source_path).path)
            except ValueError:
                return None
            match = re.match(r"^/d(/.*)$", decoded, re.IGNORECASE)
            return normalize_virtual_path(match.group(1)) if match else None
        if self._is_strm_media_source(source) and source_path.startswith("/"):
            return normalize_virtual_path(source_path)
        return None

    @staticmethod
    def _is_strm_media_source(source: dict[str, Any]) -> bool:
        return source.get("IsRemote") is True and source.get("IsInfiniteStream") is not True

    @staticmethod
    def _parse_item_id(path: str) -> str:
        match = ITEM_PATH.search(path)
        return match.group(1) if match else ""

    @staticmethod
    def _emby_prefix(path: str) -> str:
        match = re.match(r"^(.*)/(?:videos|items)/[^/]+", path, re.IGNORECASE)
        return match.group(1) if match else ""

    def _direct_stream_path(self, path: str, item_id: str) -> str:
        return f"{self._emby_prefix(path)}/Videos/{quote(item_id, safe='')}/stream"

    def _get_cache(self, key: str) -> str:
        self._cleanup_cache()
        entry = self._cache.get(key)
        if not entry or monotonic() > entry[0]:
            self._cache.pop(key, None)
            return ""
        self._cache.move_to_end(key)
        return entry[1]

    def _set_cache(self, key: str, url: str) -> None:
        self._cache[key] = (monotonic() + self.config.cache_ttl, url)
        self._cache.move_to_end(key)
        self._cleanup_cache()
        while len(self._cache) > self.config.cache_max:
            self._cache.popitem(last=False)

    def _cleanup_cache(self) -> None:
        now = monotonic()
        expired = [key for key, (expires_at, _) in self._cache.items() if now > expires_at]
        for key in expired:
            self._cache.pop(key, None)

    async def _start_server(self) -> None:
        self._validate_runtime_config(self.config)
        sock = self._bind_socket(self.config.host, self.config.port)
        server_config = uvicorn.Config(
            self,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
        server = EmbeddedUvicornServer(server_config)
        task = asyncio.create_task(server.serve(sockets=[sock]), name="strmflow-emby302-gateway")
        self._socket = sock
        self._server = server
        self._server_task = task
        for _ in range(100):
            if server.started:
                self._started_at = datetime.now(UTC)
                self._last_error = ""
                self.runtime_logs.add(
                    category="gateway302",
                    level="success",
                    message=f"302 网关已启动：{self.config.host}:{self.config.port}",
                )
                return
            if task.done():
                error = task.exception()
                raise RuntimeError(str(error or "302 网关提前退出"))
            await asyncio.sleep(0.02)
        await self._stop_server()
        raise RuntimeError("等待 302 网关启动超时")

    async def _stop_server(self) -> None:
        server, task, sock = self._server, self._server_task, self._socket
        self._server = None
        self._server_task = None
        self._socket = None
        if server:
            server.should_exit = True
        if task and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except TimeoutError:
                if server:
                    server.force_exit = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self._started_at = None

    @staticmethod
    def _bind_socket(host: str, port: int) -> socket.socket:
        last_error: OSError | None = None
        for family, sock_type, proto, _, address in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        ):
            sock = socket.socket(family, sock_type, proto)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(address)
                sock.listen(2048)
                sock.setblocking(False)
                return sock
            except OSError as exc:
                last_error = exc
                sock.close()
        raise OSError(f"无法监听 {host}:{port}：{last_error}")

    def _default_config(self) -> Emby302Config:
        return Emby302Config(
            enabled=self.settings.emby_302_enabled,
            emby_url=self.settings.emby_url.rstrip("/"),
            openlist_url=self.settings.openlist_url.rstrip("/"),
            host=self.settings.emby_302_host,
            port=self.settings.emby_302_port,
            cache_ttl=self.settings.emby_302_cache_ttl,
            cache_max=self.settings.emby_302_cache_max,
            body_buffer_max=self.settings.emby_302_body_buffer_max,
            timeout_ms=self.settings.emby_302_timeout_ms,
        )

    def _parse_config(self, value: dict[str, Any]) -> Emby302Config:
        defaults = self._default_config()
        emby_url = value.get("embyUrl", value.get("emby_url"))
        openlist_url = value.get("openlistUrl", value.get("openlist_url"))
        return Emby302Config(
            enabled=bool(value.get("enabled", defaults.enabled)),
            emby_url=(defaults.emby_url if emby_url is None else str(emby_url).strip().rstrip("/")),
            openlist_url=(
                defaults.openlist_url
                if openlist_url is None
                else str(openlist_url).strip().rstrip("/")
            ),
            host=str(value.get("host", defaults.host)).strip(),
            port=int(value.get("port", defaults.port)),
            cache_ttl=int(value.get("cacheTtl", value.get("cache_ttl", defaults.cache_ttl))),
            cache_max=int(value.get("cacheMax", value.get("cache_max", defaults.cache_max))),
            body_buffer_max=int(
                value.get("bodyBufferMax", value.get("body_buffer_max", defaults.body_buffer_max))
            ),
            timeout_ms=int(value.get("timeoutMs", value.get("timeout_ms", defaults.timeout_ms))),
        )

    def _validate_runtime_config(self, config: Emby302Config) -> None:
        if not config.host or any(character.isspace() for character in config.host):
            raise AppError(422, "302 网关监听地址格式不正确")
        for label, url in (("Emby", config.emby_url), ("OpenList", config.openlist_url)):
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise AppError(422, f"{label} 容器地址格式不正确")
        ranges = {
            "监听端口": (config.port, 1, 65_535),
            "缓存时间": (config.cache_ttl, 1, 86_400),
            "缓存数量": (config.cache_max, 1, 100_000),
            "请求体上限": (config.body_buffer_max, 1_024, 107_374_182_400),
            "请求超时": (config.timeout_ms, 1_000, 600_000),
        }
        for label, (number, minimum, maximum) in ranges.items():
            if number < minimum or number > maximum:
                raise AppError(422, f"{label}需要在 {minimum} 至 {maximum} 之间")
        if config.enabled:
            missing = []
            if not config.emby_url:
                missing.append("Emby 容器地址")
            if not self.settings.emby_api_key:
                missing.append("EMBY_API_KEY")
            if not config.openlist_url:
                missing.append("OpenList 容器地址")
            if not self.settings.openlist_token:
                missing.append("OPENLIST_TOKEN")
            if missing:
                raise AppError(422, "启用 302 网关前请配置：" + "、".join(missing))
            if config.port == self.settings.port and config.host in {
                self.settings.host,
                "0.0.0.0",
                "::",
            }:
                raise AppError(422, "302 网关端口不能与 StrmFlow 管理端口相同")

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None
