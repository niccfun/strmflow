import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.services.emby302 import Emby302Gateway
from strmflow.services.openlist import OpenListClient


def test_redirect_url_percent_encodes_unicode_path_and_query() -> None:
    value = Emby302Gateway._redirect_url(
        "https://cdn.test/视频/百花杀 01.mkv?文件名=百花杀&token=abc"
    )
    assert (
        value
        == "https://cdn.test/%E8%A7%86%E9%A2%91/%E7%99%BE%E8%8A%B1%E6%9D%80%2001.mkv?%E6%96%87%E4%BB%B6%E5%90%8D=%E7%99%BE%E8%8A%B1%E6%9D%80&token=abc"
    )


def test_video_path_accepts_emby_strm_suffix() -> None:
    assert Emby302Gateway._parse_item_id("/emby/videos/3771/stream.strm") == "3771"


def test_extract_openlist_path_accepts_public_p_prefix() -> None:
    source = {
        "Path": "https://openlist.test/p/temp_strm/TV/%E7%99%BE%E8%8A%B1%E6%9D%80/S01E01.strm",
        "IsRemote": True,
    }
    gateway = Emby302Gateway.__new__(Emby302Gateway)
    assert gateway._extract_openlist_path(source) == "/temp_strm/TV/百花杀/S01E01.strm"


def test_response_header_unicode_is_latin1_safe() -> None:
    assert Emby302Gateway._header_value("content-disposition", "文件名=百花杀") == "???=???"


class MemorySettingsRepository:
    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.link_cache: dict[str, Any] | None = None

    async def load_emby302(self) -> dict[str, Any] | None:
        return self.value

    async def save_emby302(self, value: dict[str, Any]) -> None:
        self.value = value

    async def load_emby302_link_cache(self) -> dict[str, Any] | None:
        return self.link_cache

    async def save_emby302_link_cache(self, value: dict[str, Any]) -> None:
        self.link_cache = value


def test_provider_expiry_prefers_openlist_timestamp() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC).timestamp()
    expected = now + 3_600
    actual, source = Emby302Gateway._provider_expiry(
        datetime.fromtimestamp(expected, UTC).isoformat(),
        f"https://cdn.test/video.mkv?expires=8h&dstime={int(now)}",
        now,
    )
    assert actual == expected
    assert source == "openlist"


def test_provider_expiry_understands_baidu_duration_and_dstime() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC).timestamp()
    actual, source = Emby302Gateway._provider_expiry(
        None,
        f"https://d.pcs.baidu.com/file/video.mkv?expires=8h&dstime={int(now)}",
        now,
    )
    assert actual == now + 8 * 60 * 60
    assert source == "url"


async def test_emby302_redirects_strm_stream_and_reuses_cache() -> None:
    emby_queries = 0
    fs_get_queries = 0
    link_queries: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal emby_queries, fs_get_queries
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
            fs_get_queries += 1
            return httpx.Response(500)
        if request.url.host == "openlist.test" and request.url.path == "/api/fs/link":
            path = str(request.read().decode())
            link_queries.append(path)
            if "E01.strm" in path:
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "data": {
                            "url": "http://openlist.test/manifest.strm",
                            "header": {"X-Manifest": ["yes"]},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "url": "https://cdn.test/video.mkv?token=secret",
                        "Expiration": None,
                    },
                },
            )
        if request.url.host == "openlist.test" and request.url.path == "/manifest.strm":
            assert request.headers["x-manifest"] == "yes"
            return httpx.Response(200, text="http://openlist.test/d/provider/video.mkv\n")
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
        await gateway.close()

        restored_gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            repository,  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        await restored_gateway.initialize()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restored_gateway),
            base_url="http://gateway.test",
            follow_redirects=False,
        ) as client:
            restored = await client.get(
                "/emby/Videos/item-1/stream", params={"MediaSourceId": "source-1"}
            )
        await restored_gateway.close()

    assert first.status_code == 302
    assert first.headers["location"] == "https://cdn.test/video.mkv?token=secret"
    assert second.status_code == 302
    assert restored.status_code == 302
    assert emby_queries == 1
    assert fs_get_queries == 0
    assert len(link_queries) == 2
    assert repository.link_cache
    assert len(repository.link_cache["entries"]) == 2
    snapshot = gateway.snapshot()
    assert snapshot["stats"]["redirects"] == 2
    assert snapshot["stats"]["cacheHits"] == 1
    assert snapshot["recentRedirects"][0]["targetHost"] == "cdn.test"
    assert restored_gateway.snapshot()["stats"]["restoredCacheEntries"] == 2


async def test_prewarm_keeps_latest_six_episodes_and_persists() -> None:
    settings = Settings(
        app_password="secret",
        openlist_token="token",
        emby_api_key="emby-key",
        emby_302_enabled=True,
    )
    repository = MemorySettingsRepository()
    async with (
        httpx.AsyncClient() as emby_http,
        httpx.AsyncClient(base_url=settings.openlist_url) as openlist_http,
    ):
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            repository,  # type: ignore[arg-type]
            RuntimeLogStore(),
        )

        async def resolve(path: str) -> dict[str, Any]:
            return gateway._new_cache_entry(
                f"https://cdn.test/{path.rsplit('/', 1)[-1]}",
                source_path=path,
                provider_path="/provider/video.mkv",
            )

        gateway._resolve_openlist_target_once = resolve  # type: ignore[method-assign]
        paths = [f"/library/Season 01/Demo.S01E{episode:02d}.strm" for episode in range(1, 9)]
        assert gateway.schedule_prewarm({"id": "m1", "name": "Demo"}, paths) == 6
        await asyncio.gather(*tuple(gateway._prewarm_tasks))
        await gateway.close()

    cached_paths = {key.removeprefix("path:") for key in (repository.link_cache or {})["entries"]}
    assert cached_paths == set(paths[2:])
    assert gateway.snapshot()["stats"]["prewarmedLinks"] == 6


async def test_playback_info_preserves_canonical_stream_url_and_session_query() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Items/item-1/PlaybackInfo"
        return httpx.Response(
            200,
            json={
                "PlaySessionId": "session-1",
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Path": "http://openlist.test/d/TV/Demo/E01.strm",
                        "IsRemote": True,
                        "DirectStreamUrl": (
                            "/videos/item-1/stream.strm?UserId=user-1&api_key=client-token"
                            "&MediaSourceId=source-1&PlaySessionId=session-1&Static=true"
                        ),
                    }
                ],
            },
        )

    settings = Settings(app_password="secret", openlist_token="token", emby_api_key="key")
    repository = MemorySettingsRepository()
    repository.value = {
        "embyUrl": "http://emby.test/emby",
        "openlistUrl": "http://openlist.test",
    }
    transport = httpx.MockTransport(upstream)
    async with (
        httpx.AsyncClient(transport=transport) as emby_http,
        httpx.AsyncClient(transport=transport) as openlist_http,
    ):
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            repository,  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        await gateway.initialize()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test"
        ) as client:
            response = await client.post("/emby/Items/item-1/PlaybackInfo", json={})
        await gateway.close()

    assert response.status_code == 200
    direct_stream_url = response.json()["MediaSources"][0]["DirectStreamUrl"]
    parsed = urlsplit(direct_stream_url)
    assert parsed.path == "/videos/item-1/stream.strm"
    assert "/emby/emby/" not in direct_stream_url.casefold()
    assert parse_qs(parsed.query) == {
        "UserId": ["user-1"],
        "api_key": ["client-token"],
        "MediaSourceId": ["source-1"],
        "PlaySessionId": ["session-1"],
        "Static": ["true"],
    }


async def test_zero_progress_is_acknowledged_without_erasing_upstream_resume() -> None:
    upstream_calls = 0

    async def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(204)

    settings = Settings(app_password="secret", openlist_token="token", emby_api_key="key")
    transport = httpx.MockTransport(upstream)
    async with (
        httpx.AsyncClient(transport=transport) as emby_http,
        httpx.AsyncClient(transport=transport) as openlist_http,
    ):
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            MemorySettingsRepository(),  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test"
        ) as client:
            response = await client.post(
                "/emby/Sessions/Playing/Progress",
                json={
                    "ItemId": "item-1",
                    "PlaySessionId": "session-1",
                    "PositionTicks": 0,
                },
            )
        await gateway.close()

    assert response.status_code == 204
    assert upstream_calls == 0


async def test_invalid_runtime_and_stop_position_are_not_forwarded_to_emby() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    settings = Settings(app_password="secret", openlist_token="token", emby_api_key="key")
    transport = httpx.MockTransport(upstream)
    async with (
        httpx.AsyncClient(transport=transport) as emby_http,
        httpx.AsyncClient(transport=transport) as openlist_http,
    ):
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            MemorySettingsRepository(),  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway.test"
        ) as client:
            await client.post(
                "/emby/Sessions/Playing/Progress",
                json={
                    "ItemId": "item-1",
                    "PlaySessionId": "session-1",
                    "PositionTicks": 20_000_000,
                    "RunTimeTicks": 0,
                },
            )
            await client.post(
                "/emby/Sessions/Playing/Stopped",
                json={
                    "ItemId": "item-1",
                    "PlaySessionId": "session-2",
                    "PositionTicks": 0,
                    "RunTimeTicks": 0,
                },
            )
        await gateway.close()

    assert calls[0][1]["PositionTicks"] == 20_000_000
    assert "RunTimeTicks" not in calls[0][1]
    assert "PositionTicks" not in calls[1][1]
    assert "RunTimeTicks" not in calls[1][1]


async def test_stopped_event_restores_last_positive_progress_for_strm_resume() -> None:
    calls: list[tuple[str, str, dict[str, Any], str]] = []
    user_id = "0123456789abcdef0123456789abcdef"
    stored_user_data: dict[str, Any] = {
        "PlaybackPositionTicks": 0,
        "Played": True,
        "PlayCount": 1,
        "IsFavorite": False,
    }

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal stored_user_data
        body = json.loads(request.content) if request.content else {}
        calls.append(
            (
                request.method,
                request.url.path,
                body,
                request.headers.get("x-emby-authorization", ""),
            )
        )
        if request.method == "GET" and request.url.path.endswith("/Items/item-1"):
            return httpx.Response(
                200,
                json={"RunTimeTicks": None, "UserData": dict(stored_user_data)},
            )
        if request.method == "POST" and request.url.path.endswith("/Items/item-1/UserData"):
            stored_user_data = body
        return httpx.Response(204)

    settings = Settings(app_password="secret", openlist_token="token", emby_api_key="key")
    repository = MemorySettingsRepository()
    repository.value = {
        "embyUrl": "http://emby.test/emby",
        "openlistUrl": "http://openlist.test",
    }
    transport = httpx.MockTransport(upstream)
    async with (
        httpx.AsyncClient(transport=transport) as emby_http,
        httpx.AsyncClient(transport=transport) as openlist_http,
    ):
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            repository,  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        await gateway.initialize()
        progress_ticks = 125 * 10_000_000
        common = {"ItemId": "item-1", "PlaySessionId": "session-1"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway),
            base_url="http://gateway.test",
            headers={
                "X-Emby-Authorization": (
                    f'Emby UserId="{user_id}", Client="Test", Token="client-token"'
                )
            },
        ) as client:
            progress = await client.post(
                "/emby/emby/Sessions/Playing/Progress?api_key=client-token",
                json={**common, "PositionTicks": progress_ticks},
            )
            stopped = await client.post(
                "/emby/emby/Sessions/Playing/Stopped?api_key=client-token",
                json={**common, "PositionTicks": 0},
            )
        if gateway._playstate_tasks:
            await asyncio.gather(*tuple(gateway._playstate_tasks))
        await gateway.close()

    assert progress.status_code == 204
    assert stopped.status_code == 204
    assert [(call[0], call[1]) for call in calls] == [
        ("POST", "/emby/Sessions/Playing/Progress"),
        ("POST", "/emby/Sessions/Playing/Stopped"),
        ("GET", f"/emby/Users/{user_id}/Items/item-1"),
        ("POST", f"/emby/Users/{user_id}/Items/item-1/UserData"),
        ("GET", f"/emby/Users/{user_id}/Items/item-1"),
    ]
    assert calls[0][2]["PositionTicks"] == progress_ticks
    assert calls[1][2]["PositionTicks"] == progress_ticks
    assert calls[3][2]["PlaybackPositionTicks"] == progress_ticks
    assert calls[3][2]["Played"] is False
    assert calls[3][2]["PlayCount"] == 1
    assert all(f'UserId="{user_id}"' in call[3] for call in calls)
