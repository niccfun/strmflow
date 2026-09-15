from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import socket
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter, time
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

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
from strmflow.utils.episodes import source_season_episode
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
PLAYING_PROGRESS_PATH = re.compile(r"/sessions/playing/progress$", re.IGNORECASE)
PLAYING_STOPPED_PATH = re.compile(r"/sessions/playing/stopped$", re.IGNORECASE)
CACHE_POLICY_VERSION = 2
LEGACY_CACHE_TTL = 180
DEFAULT_CACHE_TTL = 6 * 60 * 60
EXPIRY_SAFETY_SECONDS = 5 * 60
PREWARM_LATEST_COUNT = 6
CACHE_PERSIST_DEBOUNCE_SECONDS = 0.35
TICKS_PER_SECOND = 10_000_000
PLAYSTATE_CACHE_MAX = 2_000
PLAYSTATE_CACHE_TTL = 6 * 60 * 60


type LinkCacheEntry = dict[str, Any]


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
        self._cache: OrderedDict[str, LinkCacheEntry] = OrderedDict()
        self._cache_dirty = False
        self._cache_persist_task: asyncio.Task[None] | None = None
        self._prewarm_tasks: set[asyncio.Task[None]] = set()
        self._playstate_tasks: set[asyncio.Task[None]] = set()
        self._playstate_positions: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._resolution_tasks: dict[str, asyncio.Task[LinkCacheEntry]] = {}
        self._restored_cache_entries = 0
        self._prewarmed_links = 0
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
                stored = dict(stored)
                if (
                    int(stored.get("cachePolicyVersion") or 1) < CACHE_POLICY_VERSION
                    and int(stored.get("cacheTtl") or 0) == LEGACY_CACHE_TTL
                ):
                    stored["cacheTtl"] = DEFAULT_CACHE_TTL
                    stored["cachePolicyVersion"] = CACHE_POLICY_VERSION
                    await self.repository.save_emby302(stored)
                    self.runtime_logs.add(
                        category="gateway302",
                        level="success",
                        message="302 直链缓存已升级为 6 小时上限",
                    )
                self.config = self._parse_config(stored)
            except (TypeError, ValueError):
                self.runtime_logs.add(
                    category="gateway302",
                    level="warning",
                    message="已忽略无效的 302 网关持久化配置",
                )
        await self._restore_cache()

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
        for task in tuple(self._prewarm_tasks):
            task.cancel()
        if self._prewarm_tasks:
            await asyncio.gather(*self._prewarm_tasks, return_exceptions=True)
        self._prewarm_tasks.clear()
        for task in tuple(self._resolution_tasks.values()):
            task.cancel()
        if self._resolution_tasks:
            await asyncio.gather(*self._resolution_tasks.values(), return_exceptions=True)
        self._resolution_tasks.clear()
        if self._playstate_tasks:
            await asyncio.gather(*tuple(self._playstate_tasks), return_exceptions=True)
        self._playstate_tasks.clear()
        self._playstate_positions.clear()
        async with self._lock:
            await self._stop_server()
        await self._flush_cache()

    async def update(self, value: dict[str, Any]) -> dict[str, Any]:
        new_config = self._parse_config(value)
        self._validate_runtime_config(new_config)
        async with self._lock:
            old_config = self.config
            await self._flush_cache()
            await self._stop_server()
            self.config = new_config
            self._cache.clear()
            self._cache_dirty = True
            try:
                if new_config.enabled:
                    await self._start_server()
                await self.repository.save_emby302(
                    {**new_config.as_dict(), "cachePolicyVersion": CACHE_POLICY_VERSION}
                )
                await self._persist_cache()
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

    async def clear_cache(self) -> int:
        count = len(self._cache)
        self._cache.clear()
        self._cache_dirty = True
        await self._flush_cache()
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
                "restoredCacheEntries": self._restored_cache_entries,
                "prewarmedLinks": self._prewarmed_links,
                "prewarmedCacheEntries": sum(
                    bool(entry.get("prewarmed")) for entry in self._cache.values()
                ),
                "nextCacheExpiryAt": self._next_cache_expiry(),
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
            redirectTo=self._redacted_redirect(response.headers.get("location", "")),
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
        if request.method == "POST" and PLAYING_PROGRESS_PATH.search(lower_path):
            return await self._handle_playing_progress(request)
        if request.method == "POST" and PLAYING_STOPPED_PATH.search(lower_path):
            return await self._handle_playing_stopped(request)
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
        cache_key = self._request_cache_key(item_id, media_source_id)
        cached = self._get_cache(cache_key)
        if cached:
            self._cache_hits += 1
            return self._redirect(str(cached["url"]), item_id, "", True), "cache-hit"

        media_source = await self._get_emby_media_source(request.url.path, item_id, media_source_id)
        if not media_source or not media_source.get("Path"):
            return await self._proxy(request), "media-source-missing"
        openlist_path = self._extract_openlist_path(media_source)
        if not openlist_path:
            return await self._proxy(request), "non-openlist-source"

        path_key = self._path_cache_key(openlist_path)
        cached = self._get_cache(path_key)
        if cached:
            self._cache_hits += 1
            self._set_cache_entry(cache_key, cached, prewarmed=False)
            return (
                self._redirect(str(cached["url"]), item_id, openlist_path, True),
                "path-cache-hit",
            )

        try:
            entry = await self._resolve_openlist_target_once(openlist_path)
        except AppError as exc:
            if exc.status_code == 404:
                return PlainTextResponse(exc.message, status_code=502), "openlist-error"
            raise
        self._set_cache_entry(path_key, entry, prewarmed=False)
        self._set_cache_entry(cache_key, entry, prewarmed=False)
        return self._redirect(str(entry["url"]), item_id, openlist_path, False), "redirect"

    async def _resolve_openlist_target(self, openlist_path: str) -> LinkCacheEntry:
        """Resolve one Emby/OpenList media source with the shortest uncached chain."""
        timeout = self.config.timeout_ms / 1_000
        raw_url = ""
        provider_path = ""
        expiration: object = None

        if urlsplit(openlist_path).path.casefold().endswith(".strm"):
            # The Emby media source already proves that this path exists. Calling
            # fs/get before fs/link duplicates the slowest OpenList lookup, so the
            # playback path reads the one-line manifest directly.
            manifest = await self.openlist.read_text(
                openlist_path,
                base_url=self.config.openlist_url,
                timeout=timeout,
                verify_exists=False,
            )
            raw_url = next(
                (
                    line.strip()
                    for line in manifest.lstrip("\ufeff").splitlines()
                    if line.strip().startswith(("http://", "https://"))
                ),
                "",
            )
            if not raw_url:
                raise AppError(502, "STRM 文件内容不是有效媒体地址")
            provider_path = self._extract_openlist_path({"Path": raw_url, "IsRemote": True}) or ""
        else:
            # A non-STRM OpenList source can be resolved in one fs/link call.
            provider_path = openlist_path

        if provider_path:
            link = await self.openlist.direct_link_info(
                provider_path,
                base_url=self.config.openlist_url,
                timeout=timeout,
            )
            raw_url = str(link["url"])
            expiration = link.get("expiration")

        if not raw_url.startswith(("http://", "https://")):
            raise AppError(502, "OpenList 未返回有效的媒体直链")
        entry = self._new_cache_entry(
            raw_url,
            source_path=openlist_path,
            provider_path=provider_path,
            expiration=expiration,
        )
        self.runtime_logs.add(
            category="gateway302",
            level="info",
            message="OpenList 媒体地址已解析为网盘 CDN 直链",
            openListPath=provider_path or openlist_path,
            targetHost=urlsplit(raw_url).netloc,
            expirySource=entry["expirySource"],
            cacheExpiresAt=datetime.fromtimestamp(float(entry["expiresAt"]), UTC).isoformat(),
        )
        return entry

    async def _resolve_openlist_target_once(self, openlist_path: str) -> LinkCacheEntry:
        """Coalesce concurrent player/prewarm lookups for the same STRM file."""
        key = normalize_virtual_path(openlist_path)
        task = self._resolution_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._resolve_openlist_target(key),
                name="strmflow-emby302-link-resolve",
            )
            self._resolution_tasks[key] = task

            def discard(completed: asyncio.Task[LinkCacheEntry]) -> None:
                if self._resolution_tasks.get(key) is completed:
                    self._resolution_tasks.pop(key, None)

            task.add_done_callback(discard)
        return await asyncio.shield(task)

    def schedule_prewarm(
        self,
        item: dict[str, Any] | None,
        paths: list[str] | tuple[str, ...] | None,
    ) -> int:
        """Warm the latest published STRM files without delaying the sync response."""
        if not self.config.enabled:
            return 0
        candidates = {
            normalize_virtual_path(path)
            for path in paths or []
            if str(path).casefold().endswith(".strm")
        }
        selected = sorted(candidates, key=self._episode_sort_key)[-PREWARM_LATEST_COUNT:]
        if not selected:
            return 0
        task = asyncio.create_task(
            self._prewarm_paths(item or {}, selected),
            name=f"strmflow-emby302-prewarm-{(item or {}).get('id') or 'media'!s}",
        )
        self._prewarm_tasks.add(task)
        task.add_done_callback(self._prewarm_tasks.discard)
        self.runtime_logs.add(
            category="gateway302",
            level="info",
            message=f"已提交最新 {len(selected)} 个 STRM 直链预热任务",
            itemId=str((item or {}).get("id") or ""),
            mediaName=str((item or {}).get("name") or ""),
        )
        return len(selected)

    async def _prewarm_paths(self, item: dict[str, Any], paths: list[str]) -> None:
        success = 0
        skipped = 0
        failed = 0
        semaphore = asyncio.Semaphore(2)

        async def warm(path: str) -> None:
            nonlocal success, skipped, failed
            key = self._path_cache_key(path)
            if self._get_cache(key):
                skipped += 1
                return
            async with semaphore:
                try:
                    entry = await self._resolve_openlist_target_once(path)
                    self._set_cache_entry(key, entry, prewarmed=True)
                    success += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad episode must not stop the batch
                    failed += 1
                    self.runtime_logs.add(
                        category="gateway302",
                        level="warning",
                        message=f"302 直链预热失败：{str(exc)[:240]}",
                        mediaName=str(item.get("name") or ""),
                        sourcePath=path,
                    )

        await asyncio.gather(*(warm(path) for path in paths))
        if success:
            self._prewarmed_links += success
            await self._flush_cache()
        self.runtime_logs.add(
            category="gateway302",
            level="success" if not failed else "warning",
            message=(f"302 最新剧集直链预热完成：成功 {success}、已缓存 {skipped}、失败 {failed}"),
            itemId=str(item.get("id") or ""),
            mediaName=str(item.get("name") or ""),
        )

    @staticmethod
    def _episode_sort_key(path: str) -> tuple[int, int, str]:
        season, episode = source_season_episode(path)
        return season or 0, episode if episode is not None else -1, path.casefold()

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

    @staticmethod
    def _redacted_redirect(value: str) -> str:
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"}:
            return value
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

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
                direct_stream_url = self._direct_stream_url(
                    source.get("DirectStreamUrl"),
                    request.url.query,
                    item_id,
                    str(source.get("Id") or ""),
                )
                if direct_stream_url:
                    source["DirectStreamUrl"] = direct_stream_url
        return JSONResponse(
            body,
            status_code=upstream.status_code,
            headers=self._response_headers(upstream, strip_content=True),
        )

    async def _handle_playing_progress(self, request: Request) -> tuple[Response, str]:
        payload = await self._playstate_payload(request)
        position = self._position_ticks(payload)
        item_id = self._payload_text(payload, "ItemId")
        session_key = self._playstate_key(payload)

        # Some clients report a zero position immediately after a CDN redirect.
        # Passing it upstream can erase a valid resume point for duration-less STRM
        # items, so only the invalid startup report is acknowledged locally.
        if position is not None and position <= TICKS_PER_SECOND:
            self.runtime_logs.add(
                category="gateway302",
                level="info",
                message="已忽略可能覆盖续播断点的零进度上报",
                eventType="playback-progress-ignored",
                itemId=item_id,
                positionSeconds=round(position / TICKS_PER_SECOND, 1),
            )
            return Response(status_code=204), "ignored-zero-progress"

        if position is not None and session_key:
            self._remember_playstate(session_key, position)
        upstream = await self._proxy_buffered(request)
        if position is not None:
            self.runtime_logs.add(
                category="gateway302",
                level="info",
                message=f"播放进度已上报至 Emby：{self._format_position(position)}",
                eventType="playback-progress",
                itemId=item_id,
                positionSeconds=round(position / TICKS_PER_SECOND, 1),
                upstreamStatus=upstream.status_code,
            )
        return self._buffered_response(upstream), "playback-progress"

    async def _handle_playing_stopped(self, request: Request) -> tuple[Response, str]:
        payload = await self._playstate_payload(request)
        reported_position = self._position_ticks(payload)
        session_key = self._playstate_key(payload)
        remembered_position = self._take_playstate(session_key) if session_key else None
        position = reported_position
        if (position is None or position <= TICKS_PER_SECOND) and remembered_position is not None:
            position = remembered_position

        # Preserve the client's original stop event, then write an isolated final
        # checkpoint. Emby otherwise tends to reset duration-less STRM items to zero.
        upstream = await self._proxy_buffered(request)
        item_id = self._payload_text(payload, "ItemId")
        if upstream.is_success and item_id and position is not None and position > TICKS_PER_SECOND:
            headers = self._request_headers(request)
            headers.pop("content-length", None)
            headers["content-type"] = "application/json"
            task = asyncio.create_task(
                self._send_resume_checkpoint(
                    request.url.path,
                    request.url.query,
                    headers,
                    item_id,
                    position,
                ),
                name=f"strmflow-emby302-resume-{item_id}",
            )
            self._playstate_tasks.add(task)
            task.add_done_callback(self._playstate_tasks.discard)
            self.runtime_logs.add(
                category="gateway302",
                level="info",
                message=f"正在固化 Emby 续播断点：{self._format_position(position)}",
                eventType="playback-resume-checkpoint",
                itemId=item_id,
                positionSeconds=round(position / TICKS_PER_SECOND, 1),
                usedRememberedPosition=bool(
                    remembered_position is not None
                    and (reported_position is None or reported_position <= TICKS_PER_SECOND)
                ),
            )
        else:
            self.runtime_logs.add(
                category="gateway302",
                level="info",
                message="播放已停止，本次没有可写入的有效续播断点",
                eventType="playback-stopped",
                itemId=item_id,
                positionSeconds=(
                    round(position / TICKS_PER_SECOND, 1) if position is not None else None
                ),
                upstreamStatus=upstream.status_code,
            )
        return self._buffered_response(upstream), "playback-stopped"

    async def _send_resume_checkpoint(
        self,
        stopped_path: str,
        query: str,
        headers: dict[str, str],
        item_id: str,
        position: int,
    ) -> None:
        progress_path = PLAYING_STOPPED_PATH.sub("/Sessions/Playing/Progress", stopped_path)
        stopped_path = PLAYING_STOPPED_PATH.sub("/Sessions/Playing/Stopped", stopped_path)
        checkpoint = {
            "ItemId": item_id,
            "PlaySessionId": secrets.token_hex(16),
            "PositionTicks": position,
        }
        try:
            for path in (progress_path, stopped_path):
                response = await self.emby_http.post(
                    self._upstream_url(path, query),
                    headers=headers,
                    content=json.dumps(checkpoint, separators=(",", ":")).encode(),
                    timeout=self.config.timeout_ms / 1_000,
                    follow_redirects=False,
                )
                if response.status_code != 204:
                    raise httpx.HTTPStatusError(
                        f"Emby returned {response.status_code}",
                        request=response.request,
                        response=response,
                    )
            self.runtime_logs.add(
                category="gateway302",
                level="success",
                message=f"Emby 续播断点已保存：{self._format_position(position)}",
                eventType="playback-resume-saved",
                itemId=item_id,
                positionSeconds=round(position / TICKS_PER_SECOND, 1),
            )
        except (httpx.HTTPError, ValueError) as exc:
            self.runtime_logs.add(
                category="gateway302",
                level="error",
                message=f"Emby 续播断点保存失败：{str(exc)[:240]}",
                eventType="playback-resume-error",
                itemId=item_id,
                positionSeconds=round(position / TICKS_PER_SECOND, 1),
            )

    async def _playstate_payload(self, request: Request) -> dict[str, Any]:
        body = await self._read_body(request)
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _payload_text(payload: dict[str, Any], name: str) -> str:
        value = payload.get(name)
        return "" if value is None else str(value).strip()

    @staticmethod
    def _position_ticks(payload: dict[str, Any]) -> int | None:
        value = payload.get("PositionTicks")
        if value is None or isinstance(value, bool):
            return None
        try:
            position = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0, position)

    def _playstate_key(self, payload: dict[str, Any]) -> str:
        item_id = self._payload_text(payload, "ItemId")
        if not item_id:
            return ""
        session = next(
            (
                self._payload_text(payload, name)
                for name in ("PlaySessionId", "SessionId", "DeviceId")
                if self._payload_text(payload, name)
            ),
            "default",
        )
        return f"{item_id}:{session}"

    def _remember_playstate(self, key: str, position: int) -> None:
        self._cleanup_playstates()
        self._playstate_positions[key] = (position, time())
        self._playstate_positions.move_to_end(key)
        while len(self._playstate_positions) > PLAYSTATE_CACHE_MAX:
            self._playstate_positions.popitem(last=False)

    def _take_playstate(self, key: str) -> int | None:
        self._cleanup_playstates()
        stored = self._playstate_positions.pop(key, None)
        return stored[0] if stored else None

    def _cleanup_playstates(self) -> None:
        cutoff = time() - PLAYSTATE_CACHE_TTL
        while self._playstate_positions:
            first_key = next(iter(self._playstate_positions))
            if self._playstate_positions[first_key][1] >= cutoff:
                break
            self._playstate_positions.popitem(last=False)

    @staticmethod
    def _format_position(position: int) -> str:
        total_seconds = max(0, position // TICKS_PER_SECOND)
        hours, remainder = divmod(total_seconds, 3_600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

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
        # A few clients append a server URL ending in /emby to a response URL
        # that already starts with /emby. Never pass that duplicated prefix on.
        normalized_path = re.sub(
            r"^(?:/emby){2,}(?=/|$)",
            "/emby",
            normalized_path,
            flags=re.IGNORECASE,
        )
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
            # OpenList commonly exposes STRM files through either /d/... or
            # the public /p/... path. Both prefixes identify the same virtual
            # file and should be resolved through the OpenList API.
            match = re.match(r"^/(?:d|p)(/.*)$", decoded, re.IGNORECASE)
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

    @staticmethod
    def _direct_stream_url(
        upstream_value: object,
        request_query: str,
        item_id: str,
        media_source_id: str,
    ) -> str:
        """Keep Emby's canonical stream path and every playback query parameter."""
        raw = str(upstream_value or "").strip()
        path = ""
        query = ""
        if raw:
            try:
                parsed = urlsplit(raw)
                path = "/" + parsed.path.lstrip("/") if parsed.path else ""
                query = parsed.query
            except ValueError:
                path = ""

        if not path and item_id:
            path = f"/videos/{quote(item_id, safe='')}/stream"
            query = request_query
        if not path:
            return ""

        params = list(parse_qsl(query, keep_blank_values=True))
        names = {name.casefold() for name, _ in params}
        if media_source_id and "mediasourceid" not in names:
            params.append(("MediaSourceId", media_source_id))
        if "static" not in names:
            params.append(("Static", "true"))
        encoded = urlencode(params)
        return f"{path}?{encoded}" if encoded else path

    @staticmethod
    def _request_cache_key(item_id: str, media_source_id: str | None) -> str:
        return f"request:{item_id}:{media_source_id or 'default'}"

    @staticmethod
    def _path_cache_key(path: str) -> str:
        return f"path:{normalize_virtual_path(path)}"

    def _get_cache(self, key: str) -> LinkCacheEntry | None:
        self._cleanup_cache()
        entry = self._cache.get(key)
        if not entry or time() >= float(entry.get("expiresAt") or 0):
            if self._cache.pop(key, None) is not None:
                self._mark_cache_dirty()
            return None
        self._cache.move_to_end(key)
        return dict(entry)

    def _set_cache_entry(
        self,
        key: str,
        entry: LinkCacheEntry,
        *,
        prewarmed: bool,
    ) -> None:
        if time() >= float(entry.get("expiresAt") or 0):
            return
        value = dict(entry)
        value["prewarmed"] = prewarmed
        self._cache[key] = value
        self._cache.move_to_end(key)
        self._cleanup_cache()
        while len(self._cache) > self.config.cache_max:
            self._cache.popitem(last=False)
        self._mark_cache_dirty()

    def _cleanup_cache(self) -> None:
        now = time()
        expired = [
            key for key, entry in self._cache.items() if now >= float(entry.get("expiresAt") or 0)
        ]
        if not expired:
            return
        for key in expired:
            self._cache.pop(key, None)
        self._mark_cache_dirty()

    def _new_cache_entry(
        self,
        url: str,
        *,
        source_path: str,
        provider_path: str,
        expiration: object = None,
    ) -> LinkCacheEntry:
        now = time()
        provider_expiry, source = self._provider_expiry(expiration, url, now)
        configured_expiry = now + self.config.cache_ttl
        expires_at = configured_expiry
        if provider_expiry is not None:
            remaining = max(0.0, provider_expiry - now)
            margin = min(float(EXPIRY_SAFETY_SECONDS), max(5.0, remaining * 0.05))
            expires_at = min(configured_expiry, max(now + 1, provider_expiry - margin))
        return {
            "url": url,
            "cachedAt": now,
            "expiresAt": expires_at,
            "providerExpiresAt": provider_expiry,
            "expirySource": source,
            "sourcePath": normalize_virtual_path(source_path),
            "providerPath": normalize_virtual_path(provider_path) if provider_path else "",
            "prewarmed": False,
        }

    @classmethod
    def _provider_expiry(
        cls,
        official_expiration: object,
        url: str,
        now: float | None = None,
    ) -> tuple[float | None, str]:
        """Prefer OpenList's Link.Expiration, then infer common signed URL expiry."""
        current = time() if now is None else now
        official = cls._absolute_timestamp(official_expiration)
        if official and official > current:
            return official, "openlist"

        try:
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        except ValueError:
            return None, "configured"

        bases = [
            cls._absolute_timestamp((query.get(name) or [None])[0])
            for name in ("dstime", "time", "timestamp", "start_time")
        ]
        base = next((value for value in bases if value), current)
        for name in ("expires", "expire", "expiration", "expiry"):
            raw = (query.get(name) or [None])[0]
            if raw is None:
                continue
            absolute = cls._absolute_timestamp(raw)
            if absolute and absolute > current and absolute >= 1_000_000_000:
                return absolute, "url"
            duration = cls._duration_seconds(raw)
            if duration and base + duration > current:
                return base + duration, "url"

        sign = (query.get("sign") or [""])[0]
        if ":" in sign:
            absolute = cls._absolute_timestamp(sign.rsplit(":", 1)[-1])
            if absolute and absolute > current:
                return absolute, "openlist-sign"
        return None, "configured"

    @staticmethod
    def _absolute_timestamp(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, datetime):
            parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
            return parsed.timestamp()
        if isinstance(value, (int, float)):
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                return None
            while number >= 100_000_000_000:
                number /= 1_000
            return number
        text = str(value).strip()
        if not text or text in {"0", "null", "None"}:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            if not parsed.tzinfo:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        if not math.isfinite(number) or number <= 0:
            return None
        while number >= 100_000_000_000:
            number /= 1_000
        return number

    @staticmethod
    def _duration_seconds(value: object) -> float | None:
        text = str(value or "").strip().casefold()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            number = 0
        if number > 0 and number < 1_000_000_000:
            return number
        matches = list(re.finditer(r"(\d+(?:\.\d+)?)(ms|[smhd])", text))
        if not matches or "".join(match.group(0) for match in matches) != text:
            return None
        multipliers = {"ms": 0.001, "s": 1, "m": 60, "h": 3_600, "d": 86_400}
        return sum(float(match.group(1)) * multipliers[match.group(2)] for match in matches)

    def _cache_scope(self) -> str:
        return f"{self.config.emby_url.rstrip('/')}\n{self.config.openlist_url.rstrip('/')}"

    async def _restore_cache(self) -> None:
        loader = getattr(self.repository, "load_emby302_link_cache", None)
        if loader is None:
            return
        stored = await loader()
        if not isinstance(stored, dict) or stored.get("scope") != self._cache_scope():
            return
        raw_entries = stored.get("entries")
        if not isinstance(raw_entries, dict):
            return
        restored: list[tuple[str, LinkCacheEntry]] = []
        now = time()
        for key, raw in raw_entries.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "")
            try:
                expires_at = float(raw.get("expiresAt") or 0)
            except (TypeError, ValueError):
                continue
            if (
                not url.startswith(("http://", "https://"))
                or not math.isfinite(expires_at)
                or expires_at <= now
            ):
                continue
            restored.append((key, dict(raw)))
        for key, entry in sorted(restored, key=lambda row: float(row[1].get("cachedAt") or 0))[
            -self.config.cache_max :
        ]:
            self._cache[key] = entry
        self._restored_cache_entries = len(self._cache)
        if self._restored_cache_entries:
            self.runtime_logs.add(
                category="gateway302",
                level="success",
                message=f"已从 SQLite 恢复 {self._restored_cache_entries} 条有效直链缓存",
                nextExpiryAt=self._next_cache_expiry(),
            )
        if len(restored) != len(raw_entries):
            self._cache_dirty = True
            self._schedule_cache_persist()

    def _mark_cache_dirty(self) -> None:
        self._cache_dirty = True
        self._schedule_cache_persist()

    def _schedule_cache_persist(self) -> None:
        if self._cache_persist_task and not self._cache_persist_task.done():
            return
        try:
            self._cache_persist_task = asyncio.create_task(
                self._persist_cache_debounced(),
                name="strmflow-emby302-cache-persist",
            )
        except RuntimeError:
            # snapshot() can be called while an event loop is shutting down;
            # close() performs a final synchronous flush before SQLite closes.
            self._cache_persist_task = None

    async def _persist_cache_debounced(self) -> None:
        try:
            await asyncio.sleep(CACHE_PERSIST_DEBOUNCE_SECONDS)
            while self._cache_dirty:
                await self._persist_cache()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - cache persistence must not break playback
            self.runtime_logs.add(
                category="gateway302",
                level="warning",
                message=f"302 直链缓存写入 SQLite 失败：{str(exc)[:240]}",
            )
        finally:
            self._cache_persist_task = None

    async def _persist_cache(self) -> None:
        saver = getattr(self.repository, "save_emby302_link_cache", None)
        if saver is None:
            self._cache_dirty = False
            return
        self._cleanup_cache()
        payload = {
            "version": CACHE_POLICY_VERSION,
            "scope": self._cache_scope(),
            "savedAt": datetime.now(UTC).isoformat(),
            "entries": {key: dict(entry) for key, entry in self._cache.items()},
        }
        self._cache_dirty = False
        try:
            await saver(payload)
        except Exception:
            self._cache_dirty = True
            raise

    async def _flush_cache(self) -> None:
        task = self._cache_persist_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._cache_persist_task = None
        if self._cache_dirty:
            try:
                await self._persist_cache()
            except Exception as exc:  # noqa: BLE001 - shutdown/config updates remain available
                self.runtime_logs.add(
                    category="gateway302",
                    level="warning",
                    message=f"302 直链缓存持久化失败：{str(exc)[:240]}",
                )

    def _next_cache_expiry(self) -> str | None:
        expirations = [float(entry.get("expiresAt") or 0) for entry in self._cache.values()]
        if not expirations:
            return None
        return datetime.fromtimestamp(min(expirations), UTC).isoformat()

    async def _start_server(self) -> None:
        self._validate_runtime_config(self.config)
        sock = self._bind_socket(self.config.host, self.config.port)
        server_config = uvicorn.Config(
            self,
            host=self.config.host,
            port=self.config.port,
            # Reuse the parent process logger. Uvicorn's default log config
            # would reset handlers for the management app, hiding its startup
            # and runtime logs inside Docker.
            log_level="info",
            log_config=None,
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
