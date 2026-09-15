from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Self

import httpx
import pytest

from strmflow.core.config import Settings
from strmflow.services.emby import EmbyClient
from strmflow.services.system_status import SystemStatusService


class FakeSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: object) -> None:
        return None


class FakeDatabase:
    @staticmethod
    def sessions() -> FakeSession:
        return FakeSession()


class FakeOpenList:
    async def request(
        self,
        _method: str,
        path: str,
        _body: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if path == "/api/public/settings":
            return {
                "site_title": "家庭 OpenList",
                "version": "v4.2.6 (Commit: example)",
            }
        if path.startswith("/api/admin/storage/list"):
            return {
                "total": 3,
                "content": [
                    {
                        "mount_path": "/local_media",
                        "driver": "Local",
                        "status": "work",
                        "disabled": False,
                        "addition": "{}",
                    },
                    {
                        "mount_path": "/temp_strm",
                        "driver": "Strm",
                        "status": "work",
                        "disabled": False,
                        "addition": "{}",
                    },
                    {
                        "mount_path": "/bdpan",
                        "driver": "BaiduNetdisk",
                        "status": "work",
                        "disabled": False,
                        "addition": json.dumps({"AccessToken": "token-secret"}),
                    },
                ],
            }
        raise AssertionError(path)


class FakeEmby:
    async def system_info(self, *, timeout: float = 10) -> dict[str, Any]:
        assert timeout == 8
        return {
            "ServerName": "家庭影院",
            "Version": "4.8.11.0",
            "OperatingSystem": "Linux",
        }


class FakeBdpan:
    async def status(self, *, refresh: bool = False) -> dict[str, Any]:
        assert refresh is True
        return {
            "config": {"enabled": True, "binary": "bdpan", "checkIntervalMinutes": 10},
            "runtime": {
                "available": True,
                "loggedIn": True,
                "username": "网盘账号",
                "version": "3.8.7",
                "expiresAt": "2026-10-01T08:00:00+08:00",
                "schedulerRunning": True,
                "checking": False,
                "watchedCount": 2,
                "nextCheckAt": "2026-09-14T22:30:00+08:00",
                "lastCheckedAt": "2026-09-14T22:20:00+08:00",
            },
            "watches": [
                {"initialized": True, "pendingSync": False},
                {"initialized": True, "pendingSync": True},
            ],
        }

    async def quota(self, *, refresh: bool = False) -> dict[str, Any]:
        assert refresh is True
        return {
            "available": True,
            "supported": True,
            "totalBytes": 1_000,
            "usedBytes": 250,
            "freeBytes": 750,
            "usedPercent": 25.0,
            "source": "bdpan 配置 · 百度开放 API",
            "error": "",
        }


class FakeGateway:
    @staticmethod
    def snapshot() -> dict[str, Any]:
        return {
            "config": {"enabled": True},
            "stats": {
                "running": True,
                "listenAddress": "0.0.0.0:18096",
                "totalRequests": 12,
                "redirects": 10,
                "cacheHits": 7,
                "cacheHitRate": 70.0,
                "cacheEntries": 3,
                "proxyRequests": 2,
                "errors": 0,
                "uptimeSeconds": 3600,
            },
        }


class FakeMedia:
    @staticmethod
    async def list_items() -> list[dict[str, Any]]:
        return [
            {"status": "ongoing", "syncedFiles": ["01.strm", "02.strm"]},
            {"status": "completed", "syncedFiles": ["movie.strm"]},
        ]


class FakeTransfers:
    @staticmethod
    async def list() -> list[SimpleNamespace]:
        return [SimpleNamespace(status="succeeded"), SimpleNamespace(status="running")]


class FakePaths:
    list_root = "/temp_strm"
    emby_strm_root = "/local_media/emby-strm"


@pytest.mark.asyncio
async def test_status_snapshot_aggregates_services_and_redacts_storage_token() -> None:
    settings = Settings(
        openlist_url="http://admin:secret@openlist:5244/base?token=value",
        openlist_web_url="https://openlist.example.test/?auth=hidden",
        emby_url="http://emby:8096",
        emby_web_url="https://emby.example.test/",
        emby_api_key="emby-secret",
    )
    service = SystemStatusService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        FakeOpenList(),  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        FakeBdpan(),  # type: ignore[arg-type]
        FakeGateway(),  # type: ignore[arg-type]
        FakeMedia(),  # type: ignore[arg-type]
        FakeTransfers(),  # type: ignore[arg-type]
        FakePaths(),  # type: ignore[arg-type]
    )
    result = await service.snapshot(refresh=True)

    assert result["overall"] == "healthy"
    assert result["openList"]["connected"] is True
    assert result["openList"]["version"] == "v4.2.6"
    assert result["openList"]["storageCount"] == 3
    assert result["baidu"]["quota"] == {
        "available": True,
        "supported": True,
        "totalBytes": 1_000,
        "usedBytes": 250,
        "freeBytes": 750,
        "usedPercent": 25.0,
        "source": "bdpan 配置 · 百度开放 API",
        "error": "",
    }
    assert result["openList"]["internalUrl"] == "http://openlist:5244/base"
    assert result["openList"]["externalUrl"] == "https://openlist.example.test"
    assert result["emby"]["serverName"] == "家庭影院"
    assert result["baidu"]["username"] == "网盘账号"
    assert result["automation"]["pendingSyncCount"] == 1
    assert result["data"]["syncedFileCount"] == 3
    assert result["data"]["runningTransferCount"] == 1
    serialized = json.dumps(result, ensure_ascii=False)
    assert "token-secret" not in serialized
    assert "emby-secret" not in serialized
    assert "admin:secret" not in serialized


@pytest.mark.asyncio
async def test_emby_system_info_uses_api_key_and_preserves_base_path() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/System/Info"
        assert request.headers["X-Emby-Token"] == "emby-key"
        return httpx.Response(200, json={"ServerName": "Test", "Version": "4.8.11.0"})

    settings = Settings(
        emby_url="http://emby.test/emby",
        emby_api_key="emby-key",
    )
    async with httpx.AsyncClient(
        base_url=settings.emby_url.rstrip("/") + "/",
        transport=httpx.MockTransport(upstream),
    ) as client:
        payload = await EmbyClient(settings, client).system_info()

    assert payload["ServerName"] == "Test"
