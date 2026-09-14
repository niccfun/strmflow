from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text

from strmflow import __version__
from strmflow.core.config import Settings
from strmflow.infrastructure.database import Database
from strmflow.services.bdpan_automation import BdpanAutomationService
from strmflow.services.emby import EmbyClient
from strmflow.services.emby302 import Emby302Gateway
from strmflow.services.media import MediaService
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.services.transfers import TransferManager


class SystemStatusService:
    """Aggregate dependency health without exposing service credentials."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        openlist: OpenListClient,
        emby: EmbyClient,
        bdpan: BdpanAutomationService,
        emby302: Emby302Gateway,
        media: MediaService,
        transfers: TransferManager,
        path_config: PathConfigService,
    ) -> None:
        self.settings = settings
        self.database = database
        self.openlist = openlist
        self.emby = emby
        self.bdpan = bdpan
        self.emby302 = emby302
        self.media = media
        self.transfers = transfers
        self.path_config = path_config
        self._started_at = datetime.now(UTC)
        self._started_monotonic = monotonic()
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        now = monotonic()
        if not refresh and self._cache and now - self._cache[0] < 15:
            return deepcopy(self._cache[1])
        async with self._lock:
            now = monotonic()
            if not refresh and self._cache and now - self._cache[0] < 15:
                return deepcopy(self._cache[1])

            openlist, emby, bdpan_result, data = await asyncio.gather(
                self._openlist_status(),
                self._emby_status(),
                self._bdpan_status(refresh=refresh),
                self._data_status(),
            )
            baidu, automation = bdpan_result
            gateway = self._gateway_status()
            issue_count = sum(
                (
                    openlist["status"] != "online",
                    baidu["loggedIn"]
                    and baidu["quota"]["supported"]
                    and not baidu["quota"]["available"],
                    not emby["connected"],
                    not data["databaseConnected"],
                    not baidu["available"],
                    baidu["available"] and not baidu["loggedIn"],
                    gateway["enabled"] and not gateway["running"],
                )
            )
            connected_count = sum(
                (openlist["connected"], emby["connected"], data["databaseConnected"])
            )
            overall = (
                "healthy" if issue_count == 0 else "offline" if connected_count == 0 else "degraded"
            )
            result = {
                "checkedAt": datetime.now(UTC).isoformat(),
                "overall": overall,
                "issueCount": issue_count,
                "app": {
                    "status": "online",
                    "version": __version__,
                    "startedAt": self._started_at.isoformat(),
                    "uptimeSeconds": max(0, int(monotonic() - self._started_monotonic)),
                    "database": "SQLite",
                    "databaseConnected": data["databaseConnected"],
                },
                "openList": openlist,
                "emby": emby,
                "baidu": baidu,
                "automation": automation,
                "gateway302": gateway,
                "data": data,
                "paths": {
                    "sourceRoot": self.path_config.list_root,
                    "targetRoot": self.path_config.emby_strm_root,
                },
            }
            self._cache = (monotonic(), result)
            return deepcopy(result)

    async def _openlist_status(self) -> dict[str, Any]:
        started = perf_counter()
        base = {
            "connected": False,
            "status": "offline",
            "siteTitle": "OpenList",
            "version": "",
            "fullVersion": "",
            "latencyMs": 0,
            "internalUrl": self._safe_url(self.settings.openlist_url),
            "externalUrl": self._safe_url(
                self.settings.openlist_web_url or self.settings.openlist_url
            ),
            "storageCount": 0,
            "activeStorageCount": 0,
            "failedStorageCount": 0,
            "storages": [],
            "error": "",
        }
        try:
            public = await self.openlist.request("GET", "/api/public/settings", timeout=8)
            base["connected"] = True
            base["status"] = "online"
            if isinstance(public, dict):
                title = str(public.get("site_title") or "OpenList").strip()
                version = str(public.get("version") or "").strip()
                base["siteTitle"] = title[:100]
                base["fullVersion"] = version[:240]
                base["version"] = version.split(" ", 1)[0][:40]
        except Exception as exc:  # noqa: BLE001 - dependency failures are status data
            base["error"] = self._safe_error(exc, "OpenList 连接失败")
            base["latencyMs"] = self._elapsed_ms(started)
            return base

        try:
            payload = await self.openlist.request(
                "GET", "/api/admin/storage/list?page=1&per_page=1000", timeout=10
            )
            content = payload.get("content") if isinstance(payload, dict) else None
            storages = content if isinstance(content, list) else []
            public_storages = []
            active_count = 0
            failed_count = 0
            for storage in storages:
                if not isinstance(storage, dict):
                    continue
                disabled = bool(storage.get("disabled"))
                working = str(storage.get("status") or "").casefold() == "work"
                healthy = not disabled and working
                active_count += int(healthy)
                failed_count += int(not disabled and not working)
                public_storages.append(
                    {
                        "mountPath": str(storage.get("mount_path") or "/")[:500],
                        "driver": str(storage.get("driver") or "未知")[:100],
                        "status": "disabled" if disabled else "online" if working else "error",
                    }
                )
            base["storageCount"] = len(public_storages)
            base["activeStorageCount"] = active_count
            base["failedStorageCount"] = failed_count
            base["storages"] = public_storages
            if failed_count:
                base["status"] = "degraded"
        except Exception as exc:  # noqa: BLE001 - OpenList itself can still be online
            base["status"] = "degraded"
            base["error"] = self._safe_error(exc, "OpenList 存储状态读取失败")
        base["latencyMs"] = self._elapsed_ms(started)
        return base

    async def _emby_status(self) -> dict[str, Any]:
        configured = bool(self.settings.emby_url and self.settings.emby_api_key)
        base = {
            "configured": configured,
            "connected": False,
            "status": "offline" if configured else "unconfigured",
            "serverName": "",
            "version": "",
            "operatingSystem": "",
            "latencyMs": 0,
            "internalUrl": self._safe_url(self.settings.emby_url),
            "externalUrl": self._safe_url(self.settings.emby_web_url or self.settings.emby_url),
            "error": "" if configured else "尚未配置 Emby 地址或 API Key",
        }
        if not configured:
            return base
        started = perf_counter()
        try:
            payload = await self.emby.system_info(timeout=8)
            base.update(
                {
                    "connected": True,
                    "status": "online",
                    "serverName": str(payload.get("ServerName") or "Emby")[:100],
                    "version": str(payload.get("Version") or "")[:100],
                    "operatingSystem": str(payload.get("OperatingSystem") or "")[:100],
                }
            )
        except Exception as exc:  # noqa: BLE001 - dependency failures are status data
            base["error"] = self._safe_error(exc, "Emby 连接失败")
        base["latencyMs"] = self._elapsed_ms(started)
        return base

    async def _bdpan_status(self, *, refresh: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            payload = await self.bdpan.status(refresh=refresh)
            runtime = payload.get("runtime") if isinstance(payload, dict) else {}
            config = payload.get("config") if isinstance(payload, dict) else {}
            watches = payload.get("watches") if isinstance(payload, dict) else []
            runtime = runtime if isinstance(runtime, dict) else {}
            config = config if isinstance(config, dict) else {}
            watches = watches if isinstance(watches, list) else []
            available = bool(runtime.get("available"))
            logged_in = bool(runtime.get("loggedIn"))
            if available and logged_in:
                try:
                    quota = self._normalize_quota(await self.bdpan.quota(refresh=refresh))
                except Exception as exc:  # noqa: BLE001 - account state remains usable
                    quota = self._empty_quota(self._safe_error(exc, "容量状态读取失败"))
            elif available:
                quota = self._empty_quota("完成百度网盘授权后读取容量")
            else:
                quota = self._empty_quota("bdpan CLI 不可用")
                quota["supported"] = False
            baidu = {
                "available": available,
                "loggedIn": logged_in,
                "status": "online"
                if available and logged_in
                else "unauthorized"
                if available
                else "offline",
                "username": str(runtime.get("username") or "")[:100],
                "version": str(runtime.get("version") or "")[:100],
                "binary": str(runtime.get("binary") or config.get("binary") or "")[:500],
                "expiresAt": str(runtime.get("expiresAt") or "")[:100],
                "tokenExpiresIn": str(runtime.get("tokenExpiresIn") or "")[:100],
                "quota": quota,
                "error": str(runtime.get("error") or "")[:300],
            }
            automation = {
                "enabled": bool(config.get("enabled")),
                "schedulerRunning": bool(runtime.get("schedulerRunning")),
                "checking": bool(runtime.get("checking")),
                "watchedCount": int(runtime.get("watchedCount") or 0),
                "nextCheckAt": runtime.get("nextCheckAt"),
                "lastCheckedAt": runtime.get("lastCheckedAt"),
                "pendingSyncCount": sum(
                    bool(watch.get("pendingSync")) for watch in watches if isinstance(watch, dict)
                ),
                "initializedCount": sum(
                    bool(watch.get("initialized")) for watch in watches if isinstance(watch, dict)
                ),
                "checkIntervalMinutes": int(config.get("checkIntervalMinutes") or 0),
            }
            return baidu, automation
        except Exception as exc:  # noqa: BLE001 - status overview must stay available
            return (
                {
                    "available": False,
                    "loggedIn": False,
                    "status": "offline",
                    "username": "",
                    "version": "",
                    "binary": "",
                    "expiresAt": "",
                    "tokenExpiresIn": "",
                    "quota": self._empty_quota("bdpan CLI 容量状态读取失败"),
                    "error": self._safe_error(exc, "百度网盘状态读取失败"),
                },
                {
                    "enabled": False,
                    "schedulerRunning": False,
                    "checking": False,
                    "watchedCount": 0,
                    "nextCheckAt": None,
                    "lastCheckedAt": None,
                    "pendingSyncCount": 0,
                    "initializedCount": 0,
                    "checkIntervalMinutes": 0,
                },
            )

    async def _data_status(self) -> dict[str, Any]:
        database_connected = False
        try:
            async with self.database.sessions() as session:
                await session.execute(text("SELECT 1"))
            database_connected = True
        except Exception:  # noqa: BLE001, S110 - report only a safe connection boolean
            pass
        try:
            items = await self.media.list_items()
        except Exception:  # noqa: BLE001 - keep remaining counters available
            items = []
        try:
            jobs = await self.transfers.list()
        except Exception:  # noqa: BLE001 - keep remaining counters available
            jobs = []
        return {
            "databaseConnected": database_connected,
            "mediaCount": len(items),
            "ongoingCount": sum(item.get("status") == "ongoing" for item in items),
            "completedCount": sum(item.get("status") == "completed" for item in items),
            "syncedFileCount": sum(len(item.get("syncedFiles") or []) for item in items),
            "transferCount": len(jobs),
            "queuedTransferCount": sum(job.status == "queued" for job in jobs),
            "runningTransferCount": sum(job.status == "running" for job in jobs),
            "failedTransferCount": sum(job.status == "failed" for job in jobs),
        }

    def _gateway_status(self) -> dict[str, Any]:
        try:
            payload = self.emby302.snapshot()
            config = payload.get("config") if isinstance(payload, dict) else {}
            stats = payload.get("stats") if isinstance(payload, dict) else {}
            config = config if isinstance(config, dict) else {}
            stats = stats if isinstance(stats, dict) else {}
            return {
                "enabled": bool(config.get("enabled")),
                "running": bool(stats.get("running")),
                "listenAddress": str(stats.get("listenAddress") or "")[:100],
                "totalRequests": int(stats.get("totalRequests") or 0),
                "redirects": int(stats.get("redirects") or 0),
                "cacheHits": int(stats.get("cacheHits") or 0),
                "cacheHitRate": float(stats.get("cacheHitRate") or 0),
                "cacheEntries": int(stats.get("cacheEntries") or 0),
                "proxyRequests": int(stats.get("proxyRequests") or 0),
                "errors": int(stats.get("errors") or 0),
                "uptimeSeconds": int(stats.get("uptimeSeconds") or 0),
                "lastRequestAt": stats.get("lastRequestAt"),
                "lastRedirectAt": stats.get("lastRedirectAt"),
                "lastError": str(stats.get("lastError") or "")[:300],
            }
        except Exception:  # noqa: BLE001 - snapshot is best effort
            return {
                "enabled": False,
                "running": False,
                "listenAddress": "",
                "totalRequests": 0,
                "redirects": 0,
                "cacheHits": 0,
                "cacheHitRate": 0,
                "cacheEntries": 0,
                "proxyRequests": 0,
                "errors": 0,
                "uptimeSeconds": 0,
                "lastRequestAt": None,
                "lastRedirectAt": None,
                "lastError": "状态读取失败",
            }

    @staticmethod
    def _empty_quota(error: str = "暂未获取网盘容量") -> dict[str, Any]:
        return {
            "available": False,
            "supported": True,
            "totalBytes": 0,
            "usedBytes": 0,
            "freeBytes": 0,
            "usedPercent": 0,
            "source": "bdpan CLI",
            "error": error,
        }

    @classmethod
    def _normalize_quota(cls, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return cls._empty_quota("bdpan CLI 容量状态格式不正确")
        return {
            "available": bool(payload.get("available")),
            "supported": bool(payload.get("supported", True)),
            "totalBytes": max(0, cls._safe_int(payload.get("totalBytes"))),
            "usedBytes": max(0, cls._safe_int(payload.get("usedBytes"))),
            "freeBytes": max(0, cls._safe_int(payload.get("freeBytes"))),
            "usedPercent": max(0, min(100, cls._safe_float(payload.get("usedPercent")))),
            "source": "bdpan CLI",
            "error": str(payload.get("error") or "")[:300],
        }

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_url(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            parsed = urlsplit(raw)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return ""
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
        except ValueError:
            return ""

    @staticmethod
    def _safe_error(error: Exception, fallback: str) -> str:
        value = str(error).strip()
        if not value or "access_token=" in value.casefold():
            return fallback
        return value[:300]

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((perf_counter() - started) * 1000))
