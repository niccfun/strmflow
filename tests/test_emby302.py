import asyncio
from datetime import UTC, datetime
from typing import Any

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


async def test_playback_info_is_proxied_without_rewriting_request_or_response() -> None:
    request_body = b'{ "UserId": "user-1", "StartTimeTicks": 0 }'
    response_body = (
        b'{ "PlaySessionId": "session-1", "MediaSources": [{ "Id": "source-1", '
        b'"SupportsDirectPlay": false, "SupportsTranscoding": true, '
        b'"DirectStreamUrl": "/videos/item-1/original.strm?Static=false" }] }'
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Items/item-1/PlaybackInfo"
        assert request.url.query == b"api_key=client-token"
        assert request.content == request_body
        assert request.headers["x-test-control"] == "keep"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(response_body),
            headers={"Content-Type": "application/json", "X-Test-Upstream": "keep"},
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
            response = await client.post(
                "/emby/Items/item-1/PlaybackInfo?api_key=client-token",
                content=request_body,
                headers={"Content-Type": "application/json", "X-Test-Control": "keep"},
            )
        await gateway.close()

    assert response.status_code == 200
    assert response.content == response_body
    assert response.headers["x-test-upstream"] == "keep"


async def test_playback_info_rewrites_openlist_strm_to_gateway_direct_play() -> None:
    request_body = b'{"UserId":"user-1","IsPlayback":true}'

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Items/item-9/PlaybackInfo"
        assert request.url.query == b"api_key=client-token&UserId=user-1"
        assert request.content == request_body
        return httpx.Response(
            200,
            json={
                "PlaySessionId": "session-9",
                "MediaSources": [
                    {
                        "Id": "source-9",
                        "Path": "https://openlist.test/d/temp_strm/TV/Demo/Season%2001/E01.strm",
                        "IsRemote": True,
                        "SupportsDirectPlay": False,
                        "SupportsDirectStream": False,
                        "SupportsTranscoding": True,
                        "TranscodingUrl": "/Videos/item-9/master.m3u8",
                        "Container": "strm",
                    }
                ],
            },
            headers={"ETag": '"upstream"', "X-Test-Upstream": "keep"},
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
            response = await client.post(
                "/emby/Items/item-9/PlaybackInfo?api_key=client-token&UserId=user-1",
                content=request_body,
                headers={"Content-Type": "application/json"},
            )
        await gateway.close()

    assert response.status_code == 200
    payload = response.json()
    source = payload["MediaSources"][0]
    assert source["SupportsDirectPlay"] is True
    assert source["SupportsDirectStream"] is True
    assert source["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in source
    assert "Container" not in source
    assert (
        source["DirectStreamUrl"]
        == "/emby/Videos/item-9/stream?UserId=user-1&MediaSourceId=source-9&Static=true"
    )
    assert response.headers["x-test-upstream"] == "keep"
    assert "etag" not in response.headers


async def test_playstate_control_requests_are_transparently_proxied() -> None:
    calls: list[tuple[str, str, str, bytes, str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.path,
                request.url.query.decode(),
                request.content,
                request.headers.get("content-type", ""),
                request.headers.get("x-emby-authorization", ""),
            )
        )
        return httpx.Response(204, headers={"X-Emby-Playstate": "saved"})

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
        authorization = 'Emby UserId="user-1", Client="Test", Token="client-token"'
        progress_body = (
            b'{ "ItemId": "item-1", "PlaySessionId": "session-1", '
            b'"PositionTicks": 0, "RunTimeTicks": 0 }'
        )
        stopped_body = (
            b'{"ItemId":"item-1","PlaySessionId":"session-1",'
            b'"PositionTicks":1250000000,"RunTimeTicks":0}'
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway),
            base_url="http://gateway.test",
            headers={"X-Emby-Authorization": authorization},
        ) as client:
            progress = await client.post(
                "/emby/emby/Sessions/Playing/Progress?api_key=client-token&value=%E7%99%BE",
                content=progress_body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            stopped = await client.post(
                "/emby/Sessions/Playing/Stopped?api_key=client-token",
                content=stopped_body,
                headers={"Content-Type": "application/json"},
            )
        await gateway.close()

    assert progress.status_code == 204
    assert progress.headers["x-emby-playstate"] == "saved"
    assert stopped.status_code == 204
    assert calls == [
        (
            "POST",
            "/emby/Sessions/Playing/Progress",
            "api_key=client-token&value=%E7%99%BE",
            progress_body,
            "application/json; charset=utf-8",
            authorization,
        ),
        (
            "POST",
            "/emby/Sessions/Playing/Stopped",
            "api_key=client-token",
            stopped_body,
            "application/json",
            authorization,
        ),
    ]


async def test_websocket_control_channel_is_transparently_proxied(monkeypatch) -> None:
    connect_call: dict[str, Any] = {}

    class FakeUpstream:
        subprotocol = "emby"

        def __init__(self) -> None:
            self.outgoing: list[str | bytes] = []
            self.incoming: asyncio.Queue[str | bytes] = asyncio.Queue()
            self.closed: tuple[int, str] | None = None

        async def send(self, message: str | bytes) -> None:
            self.outgoing.append(message)
            await self.incoming.put(f"echo:{message}")

        async def recv(self) -> str | bytes:
            return await self.incoming.get()

        async def close(self, code: int, reason: str) -> None:
            self.closed = (code, reason)

    class FakeConnectionContext:
        def __init__(self, connection: FakeUpstream) -> None:
            self.connection = connection

        async def __aenter__(self) -> FakeUpstream:
            return self.connection

        async def __aexit__(self, *_: object) -> None:
            return None

    upstream = FakeUpstream()

    def connect(url: str, **kwargs: Any) -> FakeConnectionContext:
        connect_call.update({"url": url, **kwargs})
        return FakeConnectionContext(upstream)

    monkeypatch.setattr("strmflow.services.emby302.websocket_connect", connect)
    settings = Settings(
        app_password="secret",
        openlist_token="token",
        emby_url="http://emby.test",
        emby_api_key="key",
    )
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await queue.put({"type": "websocket.connect"})
    await queue.put({"type": "websocket.receive", "text": "hello"})
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await queue.get()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "websocket.send":
            await queue.put({"type": "websocket.disconnect", "code": 1000, "reason": "done"})

    async with (
        httpx.AsyncClient() as emby_http,
        httpx.AsyncClient() as openlist_http,
    ):
        gateway = Emby302Gateway(
            settings,
            emby_http,
            OpenListClient(settings, openlist_http),
            MemorySettingsRepository(),  # type: ignore[arg-type]
            RuntimeLogStore(),
        )
        await gateway(
            {
                "type": "websocket",
                "scheme": "ws",
                "path": "/embywebsocket",
                "raw_path": b"/embywebsocket",
                "query_string": b"api_key=client-token",
                "headers": [
                    (b"host", b"gateway.test:18096"),
                    (b"x-emby-token", b"client-token"),
                    (b"sec-websocket-protocol", b"emby"),
                ],
            },
            receive,
            send,
        )
        await gateway.close()

    assert connect_call["url"] == "ws://emby.test/embywebsocket?api_key=client-token"
    assert connect_call["subprotocols"] == ["emby"]
    assert ("x-emby-token", "client-token") in connect_call["additional_headers"]
    assert ("x-forwarded-host", "gateway.test:18096") in connect_call["additional_headers"]
    assert ("x-forwarded-proto", "http") in connect_call["additional_headers"]
    assert all(name != "sec-websocket-protocol" for name, _ in connect_call["additional_headers"])
    assert sent[0] == {"type": "websocket.accept", "subprotocol": "emby", "headers": []}
    assert sent[1] == {"type": "websocket.send", "text": "echo:hello"}
    assert upstream.outgoing == ["hello"]
    assert upstream.closed == (1000, "done")
    assert gateway.snapshot()["stats"]["activeWebSockets"] == 0
