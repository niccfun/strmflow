from typing import Any

import httpx

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.services.emby302 import Emby302Gateway
from strmflow.services.openlist import OpenListClient


class MemorySettingsRepository:
    value: dict[str, Any] | None = None

    async def load_emby302(self) -> dict[str, Any] | None:
        return self.value

    async def save_emby302(self, value: dict[str, Any]) -> None:
        self.value = value


async def test_emby302_redirects_strm_stream_and_reuses_cache() -> None:
    emby_queries = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal emby_queries
        if request.url.host == "emby.test" and request.url.path == "/emby/Items":
            emby_queries += 1
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "MediaSources": [
                                {
                                    "Id": "source-1",
                                    "Path": "http://openlist.test/d/TV/示例剧/E01.strm",
                                    "IsRemote": True,
                                }
                            ]
                        }
                    ]
                },
            )
        if request.url.host == "openlist.test" and request.url.path == "/api/fs/get":
            assert request.headers["user-agent"] == "test-player"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"raw_url": "https://cdn.test/video.mkv?token=secret"},
                },
            )
        return httpx.Response(404)

    settings = Settings(
        app_password="secret",
        openlist_url="http://unused-openlist.test",
        openlist_token="token",
        emby_url="http://unused-emby.test",
        emby_api_key="emby-key",
    )
    transport = httpx.MockTransport(upstream)
    async with (
        httpx.AsyncClient(base_url=settings.emby_url, transport=transport) as emby_http,
        httpx.AsyncClient(base_url=settings.openlist_url, transport=transport) as openlist_http,
    ):
        repository = MemorySettingsRepository()
        repository.value = {
            "embyUrl": "http://emby.test/emby",
            "openlistUrl": "http://openlist.test",
        }
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            repository,  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        await gateway.initialize()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway),
            base_url="http://gateway.test",
            follow_redirects=False,
            headers={"user-agent": "test-player"},
        ) as client:
            first = await client.get(
                "/emby/Videos/item-1/stream", params={"MediaSourceId": "source-1"}
            )
            second = await client.get(
                "/emby/Videos/item-1/stream", params={"MediaSourceId": "source-1"}
            )

    assert first.status_code == 302
    assert first.headers["location"] == "https://cdn.test/video.mkv?token=secret"
    assert second.status_code == 302
    assert emby_queries == 1
    snapshot = gateway.snapshot()
    assert snapshot["stats"]["redirects"] == 2
    assert snapshot["stats"]["cacheHits"] == 1
    assert snapshot["recentRedirects"][0]["targetHost"] == "cdn.test"
