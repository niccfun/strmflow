from datetime import UTC, datetime

import httpx

import strmflow.services.media_probe as media_probe_module
from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.infrastructure.database import Database
from strmflow.repositories.media_probes import MediaProbeRepository
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.services.emby import EmbyClient
from strmflow.services.media_probe import MediaProbeService


def test_emby_media_info_summary_uses_native_playback_payload() -> None:
    summary = EmbyClient.media_info_summary(
        {
            "MediaSources": [
                {
                    "Container": "mkv",
                    "RunTimeTicks": 28_145_280_000,
                    "Size": 1_564_259_612,
                    "MediaStreams": [
                        {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160},
                        {"Type": "Audio", "Codec": "eac3", "Channels": 6},
                    ],
                }
            ]
        }
    )

    assert summary == {
        "available": True,
        "container": "mkv",
        "videoCodec": "hevc",
        "resolution": "3840x2160",
        "durationSeconds": 2814.5,
        "sizeBytes": 1_564_259_612,
    }


def test_emby_media_info_summary_selects_requested_quality_version() -> None:
    payload = {
        "MediaSources": [
            {"Id": "1080", "Container": "mkv", "RunTimeTicks": 10, "MediaStreams": []},
            {
                "Id": "4k",
                "Container": "mkv",
                "RunTimeTicks": 20,
                "MediaStreams": [{"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160}],
            },
        ]
    }
    summary = EmbyClient.media_info_summary(payload, "4k")
    assert summary["available"] is True
    assert summary["resolution"] == "3840x2160"


async def test_emby_extract_media_info_uses_native_playback_probe() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Items/item-1/PlaybackInfo"
        assert request.headers["x-emby-token"] == "key"
        assert request.read() == b'{"IsPlayback":true}'
        return httpx.Response(
            200,
            json={
                "MediaSources": [
                    {
                        "Container": "mp4",
                        "RunTimeTicks": 10_000_000,
                        "MediaStreams": [{"Type": "Video", "Codec": "h264"}],
                    }
                ]
            },
        )

    settings = Settings(emby_url="http://emby.test", emby_api_key="key")
    async with httpx.AsyncClient(
        base_url=settings.emby_url, transport=httpx.MockTransport(upstream)
    ) as http:
        payload = await EmbyClient(settings, http).extract_media_info("item-1")
    assert EmbyClient.has_media_info(payload)


async def test_emby_lists_only_managed_strm_and_detects_missing_info() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Items"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "episode-1",
                        "Path": "/library/TV/Demo/Season 01/Demo - S01E01.strm",
                        "MediaSources": [
                            {
                                "Id": "source-1",
                                "Path": "https://openlist.example/d/media/Demo-S01E01.mp4",
                                "Container": "strm",
                                "RunTimeTicks": 0,
                                "MediaStreams": [],
                            }
                        ],
                    },
                    {
                        "Id": "episode-2",
                        "Path": "/library/TV/Demo/Season 01/Demo - S01E02.strm",
                        # Emby replaces MediaSources[].Path with the resolved URL
                        # after a successful native probe.  Item.Path is still the
                        # managed STRM path and must remain discoverable.
                        "MediaSources": [
                            {
                                "Id": "source-2",
                                "Path": "https://cdn.example/Demo-S01E02.mp4",
                                "Container": "mp4",
                                "RunTimeTicks": 10_000_000,
                                "Size": 1024,
                                "MediaStreams": [
                                    {
                                        "Type": "Video",
                                        "Codec": "h264",
                                        "Width": 1920,
                                        "Height": 1080,
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "Id": "outside",
                        "Path": "/other/Demo - S01E02.strm",
                        "MediaSources": [],
                    },
                ],
                "TotalRecordCount": 3,
            },
        )

    settings = Settings(emby_url="http://emby.test", emby_api_key="key")
    async with httpx.AsyncClient(
        base_url=settings.emby_url, transport=httpx.MockTransport(upstream)
    ) as http:
        sources = await EmbyClient(settings, http).list_strm_media_sources("/library")
    assert sources == [
        {
            "itemId": "episode-1",
            "mediaSourceId": "source-1",
            "path": "/library/TV/Demo/Season 01/Demo - S01E01.strm",
            "hasMediaInfo": False,
        },
        {
            "itemId": "episode-2",
            "mediaSourceId": "source-2",
            "path": "/library/TV/Demo/Season 01/Demo - S01E02.strm",
            "hasMediaInfo": True,
        },
    ]


def test_daily_media_scan_uses_configured_timezone_and_time() -> None:
    service = MediaProbeService.__new__(MediaProbeService)
    service.settings = Settings(app_timezone="Asia/Hong_Kong")
    service.config = {"dailyEnabled": True, "scanTime": "03:15"}
    next_scan = service._calculate_next_daily_scan(datetime(2026, 9, 15, 18, 0, tzinfo=UTC))
    assert next_scan == datetime(2026, 9, 15, 19, 15, tzinfo=UTC)


async def test_media_probe_repository_persists_only_queue_state(tmp_path) -> None:
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    await database.initialize()
    repository = MediaProbeRepository(database.sessions)
    path = "/library/示例 (2026)/Season 01/示例 - S01E01.strm"

    queued = await repository.enqueue(path)
    assert queued["status"] == "queued"
    assert await repository.mark_running(path) == 1
    await repository.retry(path, "temporary", datetime.now(UTC))
    assert (await repository.list_all())[0]["status"] == "retry"

    completed = await repository.complete(path, item_id="item-1")
    assert completed is not None
    assert completed["status"] == "complete"
    assert completed["itemId"] == "item-1"
    assert "metadata" not in completed
    assert "providerPath" not in completed
    await database.close()


async def test_media_probe_accepts_valid_playback_info_while_item_cache_catches_up(
    tmp_path, monkeypatch
) -> None:
    class FakeOpenList:
        async def get_file_info(self, _path: str) -> dict[str, int]:
            return {"size": 128}

    class FakeEmby:
        def __init__(self) -> None:
            self.find_calls = 0
            self.extract_calls = 0

        async def find_item_by_path(self, path: str) -> dict:
            self.find_calls += 1
            return {
                "Id": "item-1",
                "Path": path,
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Path": "https://cdn.example/video.mp4",
                        "Container": "strm",
                        "RunTimeTicks": 0,
                        "MediaStreams": [],
                    }
                ],
            }

        async def extract_media_info(self, *_args, **_kwargs) -> dict:
            self.extract_calls += 1
            # PlaybackInfo returns native media data before the item query cache
            # necessarily exposes Emby's asynchronous persistence.
            return {
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Container": "mkv",
                        "RunTimeTicks": 10_000_000,
                        "MediaStreams": [{"Type": "Video", "Codec": "h264"}],
                    }
                ]
            }

        def has_media_info(self, payload: dict, media_source_id: str = "") -> bool:
            return EmbyClient.has_media_info(payload, media_source_id)

        def media_info_summary(self, payload: dict, media_source_id: str = "") -> dict:
            return EmbyClient.media_info_summary(payload, media_source_id)

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    await database.initialize()
    repository = MediaProbeRepository(database.sessions)
    emby = FakeEmby()
    service = MediaProbeService(
        Settings(),
        repository,
        RuntimeSettingsRepository(database.sessions),
        FakeOpenList(),  # type: ignore[arg-type]
        emby,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    target = "/library/Demo/Season 01/Demo - S01E01.strm"
    service._active.add(target)
    await repository.enqueue(target)
    monkeypatch.setattr(media_probe_module, "PROBE_RETRY_DELAYS", ())

    await service._run_one(target)

    row = (await repository.list_all())[0]
    assert row["status"] == "complete"
    assert row["lastError"] == ""
    assert emby.extract_calls == 1
    assert emby.find_calls == 2
    await database.close()


async def test_media_probe_retries_when_playback_info_has_no_media_data(
    tmp_path, monkeypatch
) -> None:
    class FakeOpenList:
        async def get_file_info(self, _path: str) -> dict[str, int]:
            return {"size": 128}

    class FakeEmby:
        async def find_item_by_path(self, path: str) -> dict:
            return {
                "Id": "item-1",
                "Path": path,
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Path": path,
                        "Container": "strm",
                        "RunTimeTicks": 0,
                        "MediaStreams": [],
                    }
                ],
            }

        async def extract_media_info(self, *_args, **_kwargs) -> dict:
            return {
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Container": "strm",
                        "RunTimeTicks": 0,
                        "MediaStreams": [],
                    }
                ]
            }

        def has_media_info(self, payload: dict, media_source_id: str = "") -> bool:
            return EmbyClient.has_media_info(payload, media_source_id)

        def media_info_summary(self, payload: dict, media_source_id: str = "") -> dict:
            return EmbyClient.media_info_summary(payload, media_source_id)

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    await database.initialize()
    repository = MediaProbeRepository(database.sessions)
    service = MediaProbeService(
        Settings(),
        repository,
        RuntimeSettingsRepository(database.sessions),
        FakeOpenList(),  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    target = "/library/Demo/Season 01/Demo - S01E01.strm"
    service._active.add(target)
    await repository.enqueue(target)
    monkeypatch.setattr(media_probe_module, "PROBE_RETRY_DELAYS", ())

    await service._run_one(target)

    row = (await repository.list_all())[0]
    assert row["status"] == "failed"
    assert "PlaybackInfo 未返回有效媒体信息" in row["lastError"]
    await database.close()
