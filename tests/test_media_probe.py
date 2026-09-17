import asyncio
from datetime import UTC, datetime

import httpx

import strmflow.services.emby as emby_module
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


async def test_emby_lists_virtual_media_libraries_and_matches_longest_path() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Library/VirtualFolders"
        return httpx.Response(
            200,
            json=[
                {
                    "Name": "电视剧",
                    "ItemId": "library-tv",
                    "CollectionType": "tvshows",
                    "Locations": ["/library/tv"],
                },
                {
                    "Name": "国产剧",
                    "ItemId": "library-cn",
                    "CollectionType": "tvshows",
                    "Locations": ["/library/tv/国产剧"],
                },
            ],
        )

    settings = Settings(emby_url="http://emby.test", emby_api_key="key")
    async with httpx.AsyncClient(
        base_url=settings.emby_url, transport=httpx.MockTransport(upstream)
    ) as http:
        client = EmbyClient(settings, http)
        libraries = await client.list_media_libraries()

    assert [library["id"] for library in libraries] == ["library-tv", "library-cn"]
    matched = EmbyClient.media_library_for_path(
        "/library/tv/国产剧/Demo/Season 01/S01E01.strm", libraries
    )
    assert matched and matched["id"] == "library-cn"


async def test_emby_lists_only_managed_strm_and_detects_missing_info() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/emby/Items"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "episode-1",
                        "Type": "Episode",
                        "Path": "/library/TV/Demo/Season 01/Demo - S01E01.strm",
                        "ImageTags": {},
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
                        "Type": "Episode",
                        "Path": "/library/TV/Demo/Season 01/Demo - S01E02.strm",
                        "ImageTags": {"Primary": "image-tag"},
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
            "itemType": "Episode",
            "hasPrimaryImage": False,
        },
        {
            "itemId": "episode-2",
            "mediaSourceId": "source-2",
            "path": "/library/TV/Demo/Season 01/Demo - S01E02.strm",
            "hasMediaInfo": True,
            "itemType": "Episode",
            "hasPrimaryImage": True,
        },
    ]


async def test_emby_missing_scan_fetches_pages_with_bounded_concurrency(monkeypatch) -> None:
    monkeypatch.setattr(emby_module, "EMBY_SCAN_PAGE_SIZE", 2)
    items = [
        {
            "Id": f"episode-{index}",
            "Type": "Episode",
            "Path": f"/library/TV/Demo/Season 01/Demo - S01E{index:02d}.strm",
            "ImageTags": {},
            "MediaSources": [
                {
                    "Id": f"source-{index}",
                    "Container": "strm",
                    "RunTimeTicks": 0,
                    "MediaStreams": [],
                }
            ],
        }
        for index in range(1, 7)
    ]
    active = 0
    maximum_active = 0
    requested_starts: list[int] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        start = int(request.url.params["StartIndex"])
        limit = int(request.url.params["Limit"])
        requested_starts.append(start)
        if start:
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.02)
            active -= 1
        return httpx.Response(
            200,
            json={"Items": items[start : start + limit], "TotalRecordCount": len(items)},
        )

    settings = Settings(
        emby_url="http://emby.test",
        emby_api_key="key",
        media_probe_scan_concurrency=2,
    )
    async with httpx.AsyncClient(
        base_url=settings.emby_url, transport=httpx.MockTransport(upstream)
    ) as http:
        sources = await EmbyClient(settings, http).list_strm_media_sources("/library")

    assert requested_starts == [0, 2, 4]
    assert maximum_active == 2
    assert len(sources) == 6
    assert all(not source["hasMediaInfo"] for source in sources)
    assert all(not source["hasPrimaryImage"] for source in sources)


async def test_media_enhancement_queue_processes_items_serially() -> None:
    service = MediaProbeService.__new__(MediaProbeService)
    service._queue = asyncio.Queue()
    service._queued = set()
    service._active = set()
    service.runtime_logs = RuntimeLogStore()
    paths = [f"/library/Series/Season 01/S01E{index:02d}.strm" for index in range(1, 5)]
    active = 0
    maximum_active = 0
    processed: list[str] = []

    async def run_one(path: str) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        processed.append(path)
        active -= 1

    service._run_one = run_one
    for path in paths:
        service._queued.add(path)
        await service._queue.put(path)

    worker = asyncio.create_task(service._worker_loop())
    await asyncio.wait_for(service._queue.join(), timeout=1)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert processed == paths
    assert maximum_active == 1


def test_daily_media_scan_uses_configured_timezone_and_time() -> None:
    service = MediaProbeService.__new__(MediaProbeService)
    service.settings = Settings(app_timezone="Asia/Hong_Kong")
    service.config = {"dailyEnabled": True, "scanTime": "03:15"}
    next_scan = service._calculate_next_daily_scan(datetime(2026, 9, 15, 18, 0, tzinfo=UTC))
    assert next_scan == datetime(2026, 9, 15, 19, 15, tzinfo=UTC)


async def test_missing_scan_unions_media_info_and_episode_images_without_duplicates() -> None:
    class FakeEmby:
        async def list_strm_media_sources(self, _root: str, *, timeout: float) -> list[dict]:
            assert timeout == 90
            return [
                {
                    "path": "/library/Series/Season 01/S01E01.strm",
                    "hasMediaInfo": False,
                    "itemType": "Episode",
                    "hasPrimaryImage": False,
                },
                {
                    "path": "/library/Series/Season 01/S01E02.strm",
                    "hasMediaInfo": True,
                    "itemType": "Episode",
                    "hasPrimaryImage": False,
                },
                {
                    "path": "/library/Movies/Movie.strm",
                    "hasMediaInfo": True,
                    "itemType": "Movie",
                    "hasPrimaryImage": False,
                },
                {
                    "path": "/library/Series/Season 01/S01E03.strm",
                    "hasMediaInfo": True,
                    "itemType": "Episode",
                    "hasPrimaryImage": True,
                },
            ]

    class FakeRuntimeRepository:
        def __init__(self) -> None:
            self.saved: dict | None = None

        async def save_media_probe(self, value: dict) -> None:
            self.saved = dict(value)

    service = MediaProbeService.__new__(MediaProbeService)
    service.settings = Settings(media_probe_timeout=90)
    service.emby = FakeEmby()
    service.path_config = type("PathConfig", (), {"emby_strm_root": "/library"})()
    service.runtime_logs = RuntimeLogStore()
    service.runtime_repository = FakeRuntimeRepository()
    service.config = service._default_config()
    queued_paths: list[str] = []

    def schedule(paths: list[str], *, batch_id: str = "") -> int:
        assert batch_id
        queued_paths.extend(paths)
        return len(paths)

    service.schedule = schedule

    await service._scan_missing_media()

    assert set(queued_paths) == {
        "/library/Series/Season 01/S01E01.strm",
        "/library/Series/Season 01/S01E02.strm",
    }
    assert service.config["lastScannedCount"] == 4
    assert service.config["lastMissingMediaInfoCount"] == 1
    assert service.config["lastMissingImageCount"] == 2
    assert service.config["lastMissingCount"] == 2
    batch = service.config["scanBatches"][0]
    assert batch["scannedCount"] == 4
    assert batch["queuedCount"] == 2
    assert len(batch["items"]) == 2
    assert service.runtime_repository.saved == service.config


async def test_missing_scan_only_queues_episode_images_from_selected_libraries() -> None:
    class FakeEmby:
        async def list_strm_media_sources(self, _root: str, *, timeout: float) -> list[dict]:
            return [
                {
                    "path": "/library/国产剧/A/Season 01/S01E01.strm",
                    "hasMediaInfo": True,
                    "itemType": "Episode",
                    "hasPrimaryImage": False,
                },
                {
                    "path": "/library/欧美剧/B/Season 01/S01E01.strm",
                    "hasMediaInfo": True,
                    "itemType": "Episode",
                    "hasPrimaryImage": False,
                },
            ]

        async def list_media_libraries(self, *, timeout: float) -> list[dict]:
            return [
                {
                    "id": "cn",
                    "name": "国产剧",
                    "collectionType": "tvshows",
                    "locations": ["/library/国产剧"],
                },
                {
                    "id": "us",
                    "name": "欧美剧",
                    "collectionType": "tvshows",
                    "locations": ["/library/欧美剧"],
                },
            ]

        media_library_for_path = staticmethod(EmbyClient.media_library_for_path)

    class FakeRuntimeRepository:
        async def save_media_probe(self, _value: dict) -> None:
            pass

    service = MediaProbeService.__new__(MediaProbeService)
    service.settings = Settings(media_probe_timeout=90)
    service.emby = FakeEmby()
    service.path_config = type("PathConfig", (), {"emby_strm_root": "/library"})()
    service.runtime_logs = RuntimeLogStore()
    service.runtime_repository = FakeRuntimeRepository()
    service.config = service._default_config()
    service.config["episodeImageLibraryIds"] = ["cn"]
    queued_paths: list[str] = []

    def schedule(paths: list[str], *, batch_id: str = "") -> int:
        queued_paths.extend(paths)
        return len(paths)

    service.schedule = schedule
    await service._scan_missing_media()

    assert queued_paths == ["/library/国产剧/A/Season 01/S01E01.strm"]
    assert service.config["lastMissingImageCount"] == 1


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


async def test_media_probe_keeps_existing_media_info_and_only_adds_missing_episode_image(
    tmp_path, monkeypatch
) -> None:
    class FakeOpenList:
        async def get_file_info(self, _path: str) -> dict[str, int]:
            return {"size": 128}

    class FakeEmby:
        def __init__(self) -> None:
            self.extract_calls = 0

        async def find_item_by_path(self, path: str) -> dict:
            return {
                "Id": "item-1",
                "Type": "Episode",
                "Path": path,
                "ImageTags": {},
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Path": "https://cdn.example/video.mp4",
                        "Container": "mp4",
                        "RunTimeTicks": 600_000_000,
                        "MediaStreams": [{"Type": "Video", "Codec": "h264"}],
                    }
                ],
            }

        async def extract_media_info(self, *_args, **_kwargs) -> dict:
            self.extract_calls += 1
            raise AssertionError("existing media info must not be probed again")

        def has_media_info(self, payload: dict, media_source_id: str = "") -> bool:
            return EmbyClient.has_media_info(payload, media_source_id)

        def media_info_summary(self, payload: dict, media_source_id: str = "") -> dict:
            return EmbyClient.media_info_summary(payload, media_source_id)

    class FakeEpisodeImages:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def ensure_primary_image(
            self, _item: dict, target_path: str, *, duration_seconds: float
        ):
            from strmflow.services.episode_images import EpisodeImageResult

            self.calls.append((target_path, duration_seconds))
            return EpisodeImageResult("created", image_bytes=1024, seek_seconds=21)

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    await database.initialize()
    repository = MediaProbeRepository(database.sessions)
    emby = FakeEmby()
    episode_images = FakeEpisodeImages()
    service = MediaProbeService(
        Settings(),
        repository,
        RuntimeSettingsRepository(database.sessions),
        FakeOpenList(),  # type: ignore[arg-type]
        emby,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
        episode_images,  # type: ignore[arg-type]
    )
    target = "/library/Demo/Season 01/Demo - S01E01.strm"
    service._active.add(target)
    await repository.enqueue(target)
    monkeypatch.setattr(media_probe_module, "PROBE_RETRY_DELAYS", ())

    await service._run_one(target)

    row = (await repository.list_all())[0]
    assert row["status"] == "complete"
    assert emby.extract_calls == 0
    assert episode_images.calls == [(target, 60.0)]
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
                "Type": "Episode",
                "ImageTags": {},
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

    class FakeEpisodeImages:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def ensure_primary_image(
            self, _item: dict, target_path: str, *, duration_seconds: float
        ):
            from strmflow.services.episode_images import EpisodeImageResult

            self.calls.append((target_path, duration_seconds))
            return EpisodeImageResult("created", image_bytes=1024, seek_seconds=10)

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    await database.initialize()
    repository = MediaProbeRepository(database.sessions)
    episode_images = FakeEpisodeImages()
    service = MediaProbeService(
        Settings(),
        repository,
        RuntimeSettingsRepository(database.sessions),
        FakeOpenList(),  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
        episode_images,  # type: ignore[arg-type]
    )
    target = "/library/Demo/Season 01/Demo - S01E01.strm"
    service._active.add(target)
    await repository.enqueue(target)
    monkeypatch.setattr(media_probe_module, "PROBE_RETRY_DELAYS", ())

    await service._run_one(target)

    row = (await repository.list_all())[0]
    assert row["status"] == "failed"
    assert "PlaybackInfo 未返回有效媒体信息" in row["lastError"]
    assert episode_images.calls == [(target, 0.0)]
    await database.close()
