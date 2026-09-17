from __future__ import annotations

import asyncio
import math
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.services.emby import EmbyClient
from strmflow.services.openlist import OpenListClient
from strmflow.utils.paths import normalize_virtual_path

OPENLIST_DOWNLOAD_PATH = re.compile(r"^/(?:d|p)(/.*)$", re.IGNORECASE)
URL_IN_ERROR = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EpisodeImageResult:
    status: str
    image_bytes: int = 0
    seek_seconds: float = 0


@dataclass(frozen=True, slots=True)
class FfmpegInput:
    url: str
    headers: dict[str, str]


class EpisodeImageService:
    """Capture one representative remote video frame and store it as an Emby episode image."""

    def __init__(
        self,
        settings: Settings,
        openlist: OpenListClient,
        emby: EmbyClient,
        runtime_logs: RuntimeLogStore,
    ) -> None:
        self.settings = settings
        self.openlist = openlist
        self.emby = emby
        self.runtime_logs = runtime_logs

    async def ensure_primary_image(
        self,
        item: dict[str, Any],
        target_path: str,
        *,
        duration_seconds: float,
    ) -> EpisodeImageResult:
        if str(item.get("Type") or "").casefold() != "episode":
            return EpisodeImageResult("not-episode")
        item_id = str(item.get("Id") or "")
        if not item_id:
            raise RuntimeError("Emby 剧集缺少条目 ID，无法生成集图片")
        if self.emby.has_primary_image(item):
            return EpisodeImageResult("exists")

        current = await self.emby.get_item(item_id)
        if current and self.emby.has_primary_image(current):
            return EpisodeImageResult("exists")

        media_input = await self._resolve_media_input(target_path)
        resolved_duration = max(0.0, float(duration_seconds or 0))
        duration_source = "emby"
        if resolved_duration <= 0:
            duration_source = "ffprobe"
            self.runtime_logs.add(
                category="episode-image",
                level="info",
                message=f"Emby 未提供时长，正在读取视频总时长：{PurePosixPath(target_path).name}",
                targetPath=target_path,
                embyItemId=item_id,
            )
            resolved_duration = await self._probe_duration(media_input)
        seek_seconds = self._seek_seconds(resolved_duration)
        self.runtime_logs.add(
            category="episode-image",
            level="info",
            message=f"开始截取剧集图片：{PurePosixPath(target_path).name}",
            targetPath=target_path,
            embyItemId=item_id,
            durationSeconds=round(resolved_duration, 3),
            durationSource=duration_source,
            seekPercent=self.settings.episode_image_seek_percent,
            seekSeconds=seek_seconds,
            maxWidth=self.settings.episode_image_max_width,
            jpegQuality=self.settings.episode_image_jpeg_quality,
            sourceHost=urlsplit(media_input.url).netloc,
        )
        image = await self._capture_frame(media_input, seek_seconds)

        # A metadata provider may have populated an image while ffmpeg was running.
        # Recheck immediately before upload so an official episode image always wins.
        current = await self.emby.get_item(item_id)
        if current and self.emby.has_primary_image(current):
            self.runtime_logs.add(
                category="episode-image",
                level="info",
                message=f"剧集图片已由 Emby 补全，跳过截图上传：{PurePosixPath(target_path).name}",
                targetPath=target_path,
                embyItemId=item_id,
            )
            return EpisodeImageResult("exists", seek_seconds=seek_seconds)

        await self.emby.upload_primary_image(
            item_id,
            image,
            timeout=min(60, self.settings.episode_image_timeout),
        )
        self.runtime_logs.add(
            category="episode-image",
            level="success",
            message=f"剧集截图已上传到 Emby：{PurePosixPath(target_path).name}",
            targetPath=target_path,
            embyItemId=item_id,
            imageBytes=len(image),
            seekSeconds=seek_seconds,
        )
        return EpisodeImageResult("created", len(image), seek_seconds)

    async def _resolve_media_input(self, target_path: str) -> FfmpegInput:
        # MediaProbeService has already verified that this STRM exists and is
        # non-empty. Avoid a duplicate /api/fs/get before fetching its content.
        manifest = await self.openlist.read_text(target_path, verify_exists=False)
        media_url = next(
            (
                line.strip()
                for line in manifest.lstrip("\ufeff").splitlines()
                if line.strip().startswith(("http://", "https://"))
            ),
            "",
        )
        if not media_url:
            raise RuntimeError("STRM 文件内容不是有效媒体地址")

        provider_path = self._openlist_path(media_url)
        if not provider_path:
            return FfmpegInput(media_url, {})
        link = await self.openlist.direct_link_info(provider_path)
        return FfmpegInput(str(link["url"]), self._normalize_headers(link.get("headers")))

    async def _capture_frame(self, media_input: FfmpegInput, seek_seconds: float) -> bytes:
        attempts = list(dict.fromkeys((seek_seconds, self._fallback_seek(seek_seconds))))
        errors: list[str] = []
        for seek in attempts:
            try:
                return await self._run_ffmpeg(media_input, seek)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry one earlier timestamp
                errors.append(str(exc))
        detail = errors[-1] if errors else "未知错误"
        raise RuntimeError(f"ffmpeg 截取剧集图片失败：{detail}")

    async def _probe_duration(self, media_input: FfmpegInput) -> float:
        command = self._ffprobe_command(media_input)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"未找到 ffprobe：{self.settings.ffprobe_binary}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.episode_image_timeout
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            raise
        except TimeoutError as exc:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()
            raise RuntimeError(
                f"ffprobe 读取视频时长超过 {self.settings.episode_image_timeout} 秒"
            ) from exc

        if process.returncode != 0:
            raise RuntimeError(
                f"ffprobe 读取视频时长失败：{self._safe_error(stderr.decode('utf-8', 'replace'))}"
            )
        durations: list[float] = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            try:
                value = float(line.strip())
            except ValueError:
                continue
            if math.isfinite(value) and value > 0:
                durations.append(value)
        if not durations:
            raise RuntimeError("ffprobe 未返回有效视频总时长")
        return max(durations)

    async def _run_ffmpeg(self, media_input: FfmpegInput, seek_seconds: float) -> bytes:
        command = self._ffmpeg_command(media_input, seek_seconds)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"未找到 ffmpeg：{self.settings.ffmpeg_binary}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.episode_image_timeout
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            raise
        except TimeoutError as exc:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()
            raise RuntimeError(f"ffmpeg 截图超过 {self.settings.episode_image_timeout} 秒") from exc

        if process.returncode != 0:
            raise RuntimeError(self._safe_error(stderr.decode("utf-8", "replace")))
        if len(stdout) > self.settings.episode_image_max_bytes:
            raise RuntimeError(f"截图超过大小限制：{len(stdout)} 字节")
        if (
            len(stdout) < 4
            or not stdout.startswith(b"\xff\xd8")
            or not stdout.endswith(b"\xff\xd9")
        ):
            raise RuntimeError("ffmpeg 未返回有效 JPEG 图片")
        return stdout

    def _ffmpeg_command(self, media_input: FfmpegInput, seek_seconds: float) -> list[str]:
        command = [
            self.settings.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-rw_timeout",
            str(self.settings.episode_image_timeout * 1_000_000),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
            "-ss",
            f"{seek_seconds:.3f}",
        ]
        if media_input.headers:
            command.extend(("-headers", self._header_block(media_input.headers)))
        command.extend(
            (
                "-i",
                media_input.url,
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                "1",
                "-vf",
                (
                    "thumbnail=24,"
                    f"scale=w='min({self.settings.episode_image_max_width},iw)':"
                    "h=-2:flags=lanczos,format=yuvj420p"
                ),
                "-threads",
                "1",
                "-c:v",
                "mjpeg",
                "-q:v",
                str(self.settings.episode_image_jpeg_quality),
                "-f",
                "image2pipe",
                "pipe:1",
            )
        )
        return command

    def _ffprobe_command(self, media_input: FfmpegInput) -> list[str]:
        command = [
            self.settings.ffprobe_binary,
            "-v",
            "error",
            "-rw_timeout",
            str(self.settings.episode_image_timeout * 1_000_000),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
        ]
        if media_input.headers:
            command.extend(("-headers", self._header_block(media_input.headers)))
        command.extend(
            (
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration:format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                media_input.url,
            )
        )
        return command

    def _seek_seconds(self, duration_seconds: float) -> float:
        duration = max(0.0, float(duration_seconds or 0))
        if duration <= 0:
            raise RuntimeError("无法按进度百分比截图：视频总时长无效")
        return round(duration * self.settings.episode_image_seek_percent / 100, 3)

    def _fallback_seek(self, primary: float) -> float:
        # Keep the retry tied to the full duration as well: by default it moves
        # from 30% to 25%, rather than falling back to an arbitrary fixed second.
        fallback_percent = max(1, self.settings.episode_image_seek_percent - 5)
        return round(primary * fallback_percent / self.settings.episode_image_seek_percent, 3)

    @staticmethod
    def _openlist_path(url: str) -> str:
        try:
            path = unquote(urlsplit(url).path)
        except ValueError:
            return ""
        match = OPENLIST_DOWNLOAD_PATH.match(path)
        return normalize_virtual_path(match.group(1)) if match else ""

    @staticmethod
    def _normalize_headers(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            name = str(raw_name).strip()
            selected = raw_value[0] if isinstance(raw_value, list) and raw_value else raw_value
            header_value = str(selected or "").strip()
            if (
                not name
                or not header_value
                or "\r" in name
                or "\n" in name
                or "\r" in header_value
                or "\n" in header_value
            ):
                continue
            result[name] = header_value
        return result

    @staticmethod
    def _header_block(headers: dict[str, str]) -> str:
        return "".join(f"{name}: {value}\r\n" for name, value in headers.items())

    @staticmethod
    def _safe_error(value: str) -> str:
        compact = " ".join(value.split())
        compact = URL_IN_ERROR.sub("<media-url>", compact)
        return compact[-300:] or "ffmpeg 返回非零状态"
