import base64
from pathlib import Path

import httpx

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.services.emby import EmbyClient
from strmflow.services.episode_images import EpisodeImageService, FfmpegInput


async def test_emby_uploads_base64_primary_image() -> None:
    jpeg = b"\xff\xd8image-bytes\xff\xd9"

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/emby/Items/episode-1/Images/Primary"
        assert request.headers["x-emby-token"] == "key"
        assert request.headers["content-type"] == "image/jpeg"
        assert request.read() == base64.b64encode(jpeg)
        return httpx.Response(204)

    settings = Settings(emby_url="http://emby.test", emby_api_key="key")
    async with httpx.AsyncClient(
        base_url=settings.emby_url, transport=httpx.MockTransport(upstream)
    ) as http:
        await EmbyClient(settings, http).upload_primary_image("episode-1", jpeg)


async def test_episode_image_resolves_openlist_captures_and_uploads(monkeypatch) -> None:
    class FakeOpenList:
        async def read_text(self, path: str, *, verify_exists: bool) -> str:
            assert path == "/library/Demo/Season 01/Demo - S01E01.strm"
            assert verify_exists is False
            return "http://openlist:5244/d/media/Demo%20S01E01.mp4\n"

        async def direct_link_info(self, path: str) -> dict:
            assert path == "/media/Demo S01E01.mp4"
            return {
                "url": "https://cdn.example/video.mp4?token=secret",
                "headers": {"User-Agent": ["netdisk-client"], "Empty": []},
            }

    class FakeEmby:
        def __init__(self) -> None:
            self.uploaded: tuple[str, bytes] | None = None
            self.get_calls = 0

        @staticmethod
        def has_primary_image(payload: dict) -> bool:
            return EmbyClient.has_primary_image(payload)

        async def get_item(self, item_id: str) -> dict:
            self.get_calls += 1
            return {"Id": item_id, "Type": "Episode", "ImageTags": {}}

        async def upload_primary_image(self, item_id: str, image: bytes, *, timeout: float) -> None:
            assert timeout == 60
            self.uploaded = item_id, image

    emby = FakeEmby()
    service = EpisodeImageService(
        Settings(episode_image_timeout=120),
        FakeOpenList(),  # type: ignore[arg-type]
        emby,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    jpeg = b"\xff\xd8captured-frame\xff\xd9"
    captured: list[tuple[FfmpegInput, float]] = []

    async def capture(media_input: FfmpegInput, seek_seconds: float) -> bytes:
        captured.append((media_input, seek_seconds))
        return jpeg

    monkeypatch.setattr(service, "_capture_frame", capture)
    result = await service.ensure_primary_image(
        {"Id": "episode-1", "Type": "Episode", "ImageTags": {}},
        "/library/Demo/Season 01/Demo - S01E01.strm",
        duration_seconds=1000,
    )

    assert result.status == "created"
    assert result.image_bytes == len(jpeg)
    assert result.seek_seconds == 300
    assert captured == [
        (
            FfmpegInput(
                "https://cdn.example/video.mp4?token=secret",
                {"User-Agent": "netdisk-client"},
            ),
            300,
        )
    ]
    assert emby.get_calls == 2
    assert emby.uploaded == ("episode-1", jpeg)


async def test_episode_image_never_overwrites_existing_primary() -> None:
    class UnusedOpenList:
        async def read_text(self, _path: str, **_kwargs) -> str:
            raise AssertionError("existing images must not read the STRM")

    class FakeEmby:
        @staticmethod
        def has_primary_image(payload: dict) -> bool:
            return EmbyClient.has_primary_image(payload)

        async def get_item(self, _item_id: str) -> dict:
            raise AssertionError("the initial item already proves the image exists")

    service = EpisodeImageService(
        Settings(),
        UnusedOpenList(),  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    result = await service.ensure_primary_image(
        {"Id": "episode-1", "Type": "Episode", "ImageTags": {"Primary": "tag"}},
        "/library/Demo.strm",
        duration_seconds=100,
    )
    assert result.status == "exists"


async def test_episode_image_rechecks_before_upload_when_emby_populates_image(monkeypatch) -> None:
    class FakeOpenList:
        async def read_text(self, _path: str, *, verify_exists: bool) -> str:
            assert verify_exists is False
            return "https://cdn.example/video.mp4\n"

    class FakeEmby:
        def __init__(self) -> None:
            self.get_calls = 0

        @staticmethod
        def has_primary_image(payload: dict) -> bool:
            return EmbyClient.has_primary_image(payload)

        async def get_item(self, item_id: str) -> dict:
            self.get_calls += 1
            return {
                "Id": item_id,
                "Type": "Episode",
                "ImageTags": {"Primary": "provider-tag"} if self.get_calls == 2 else {},
            }

        async def upload_primary_image(self, *_args, **_kwargs) -> None:
            raise AssertionError("a provider image must never be overwritten")

    emby = FakeEmby()
    service = EpisodeImageService(
        Settings(),
        FakeOpenList(),  # type: ignore[arg-type]
        emby,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )

    async def capture(_media_input: FfmpegInput, _seek_seconds: float) -> bytes:
        return b"\xff\xd8captured-frame\xff\xd9"

    monkeypatch.setattr(service, "_capture_frame", capture)
    result = await service.ensure_primary_image(
        {"Id": "episode-1", "Type": "Episode", "ImageTags": {}},
        "/library/Demo.strm",
        duration_seconds=100,
    )

    assert result.status == "exists"
    assert emby.get_calls == 2


async def test_ffmpeg_runner_accepts_a_single_jpeg(tmp_path: Path) -> None:
    fake_ffmpeg = tmp_path / "fake-ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xd8frame\\xff\\xd9')\n",
        encoding="utf-8",
    )
    fake_ffmpeg.chmod(0o755)
    service = EpisodeImageService(
        Settings(ffmpeg_binary=str(fake_ffmpeg)),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )

    result = await service._run_ffmpeg(FfmpegInput("https://cdn.example/video.mp4", {}), 42)
    assert result == b"\xff\xd8frame\xff\xd9"


async def test_ffprobe_reads_full_duration_for_percentage_seek(tmp_path: Path) -> None:
    fake_ffprobe = tmp_path / "fake-ffprobe"
    fake_ffprobe.write_text("#!/bin/sh\nprintf '3599.875\\n3600.125\\n'\n", encoding="utf-8")
    fake_ffprobe.chmod(0o755)
    service = EpisodeImageService(
        Settings(ffprobe_binary=str(fake_ffprobe)),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )

    duration = await service._probe_duration(FfmpegInput("https://cdn.example/video.mp4", {}))

    assert duration == 3600.125
    assert service._seek_seconds(duration) == 1080.037


def test_ffmpeg_command_uses_fast_seek_headers_and_bounded_scale() -> None:
    service = EpisodeImageService(
        Settings(ffmpeg_binary="FFMPEG"),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    command = service._ffmpeg_command(
        FfmpegInput("https://cdn.example/video.mp4", {"User-Agent": "client"}), 123.456
    )

    assert command[0] == "FFMPEG"
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-ss") + 1] == "123.456"
    assert command[command.index("-headers") + 1] == "User-Agent: client\r\n"
    assert "thumbnail=24" in command[command.index("-vf") + 1]
    assert "min(1920,iw)" in command[command.index("-vf") + 1]
    assert "format=yuvj420p" in command[command.index("-vf") + 1]
    assert command[command.index("-q:v") + 1] == "2"


def test_episode_image_seek_uses_exact_full_duration_percentage() -> None:
    service = EpisodeImageService(
        Settings(episode_image_seek_percent=30),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )

    assert service._seek_seconds(2) == 0.6
    assert service._seek_seconds(0.1) == 0.03
    assert service._seek_seconds(3600) == 1080
    assert service._fallback_seek(1080) == 900
