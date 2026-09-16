from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.repositories.media_probes import MediaProbeRepository
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.schemas.api import MediaProbeConfigUpdate
from strmflow.services.emby import EmbyClient
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.utils.episodes import source_season_episode
from strmflow.utils.paths import normalize_virtual_path

PROBE_RETRY_DELAYS = (30, 120, 300)


class EmptyStrmError(RuntimeError):
    pass


class MediaProbeService:
    """Ask Emby to extract and persist new STRM media info in a background queue."""

    def __init__(
        self,
        settings: Settings,
        repository: MediaProbeRepository,
        runtime_repository: RuntimeSettingsRepository,
        openlist: OpenListClient,
        emby: EmbyClient,
        path_config: PathConfigService,
        runtime_logs: RuntimeLogStore,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.runtime_repository = runtime_repository
        self.openlist = openlist
        self.emby = emby
        self.path_config = path_config
        self.runtime_logs = runtime_logs
        self.config: dict[str, Any] = self._default_config()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._active: set[str] = set()
        self._worker: asyncio.Task[None] | None = None
        self._scheduler: asyncio.Task[None] | None = None
        self._scan_task: asyncio.Task[None] | None = None
        self._scheduler_wake = asyncio.Event()
        self._next_daily_scan_at: datetime | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def initialize(self) -> None:
        stored = await self.runtime_repository.load_media_probe()
        if stored:
            self.config = self._normalize_config(stored)
        rows = await self.repository.list_all()
        pending: list[tuple[str, float]] = []
        now = datetime.now(UTC)
        if self.settings.media_probe_enabled:
            for row in rows:
                if row.get("status") in {"complete", "failed"}:
                    continue
                next_at = self._parse_datetime(row.get("nextAttemptAt"))
                delay = max(0.0, (next_at - now).total_seconds()) if next_at else 0.0
                path = str(row["targetPath"])
                self._active.add(path)
                pending.append((path, delay))
        self._worker = asyncio.create_task(self._worker_loop(), name="emby-media-info-worker")
        self._next_daily_scan_at = self._calculate_next_daily_scan()
        self._scheduler = asyncio.create_task(
            self._daily_scheduler_loop(), name="emby-media-info-daily-scheduler"
        )
        for path, delay in sorted(
            pending, key=lambda item: self._episode_sort_key(item[0]), reverse=True
        ):
            self._schedule_put(path, delay)
        self.runtime_logs.add(
            category="media-probe",
            level="success",
            message="Emby 原生媒体信息提取队列已启动",
            resumedTaskCount=len(pending),
            delaySeconds=self.settings.media_probe_delay_seconds,
            concurrency=1,
            dailyEnabled=self.config["dailyEnabled"],
            dailyScanTime=self.config["scanTime"],
            timezone=self.settings.app_timezone,
            nextDailyScanAt=self._iso(self._next_daily_scan_at),
        )

    async def close(self) -> None:
        self._closed = True
        if self._worker:
            self._worker.cancel()
        if self._scheduler:
            self._scheduler.cancel()
        for task in tuple(self._tasks):
            task.cancel()
        pending = [task for task in [self._worker, self._scheduler, *self._tasks] if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self.runtime_logs.add(
            category="media-probe", level="success", message="Emby 媒体信息提取队列已停止"
        )

    async def status(self) -> dict[str, Any]:
        counts = await self.repository.status_counts()
        return {
            "config": {
                "dailyEnabled": bool(self.config["dailyEnabled"]),
                "scanTime": str(self.config["scanTime"]),
                "timezone": self.settings.app_timezone,
            },
            "runtime": {
                "schedulerRunning": bool(self._scheduler and not self._scheduler.done()),
                "scanning": bool(self._scan_task and not self._scan_task.done()),
                "nextScanAt": self._iso(self._next_daily_scan_at),
                "lastScanAt": self.config.get("lastScanAt"),
                "lastScannedCount": int(self.config.get("lastScannedCount") or 0),
                "lastMissingCount": int(self.config.get("lastMissingCount") or 0),
                "lastQueuedCount": int(self.config.get("lastQueuedCount") or 0),
                "lastError": str(self.config.get("lastError") or ""),
                "queueCounts": counts,
            },
        }

    async def update_config(self, value: MediaProbeConfigUpdate) -> dict[str, Any]:
        if value.daily_enabled and not self.settings.media_probe_enabled:
            raise AppError(409, "MEDIA_PROBE_ENABLED 已关闭，无法启用每日扫描")
        normalized = self._normalize_config(value.model_dump(by_alias=True), keep_runtime=False)
        self.config.update(
            {
                "dailyEnabled": normalized["dailyEnabled"],
                "scanTime": normalized["scanTime"],
            }
        )
        await self.runtime_repository.save_media_probe(self.config)
        self._next_daily_scan_at = self._calculate_next_daily_scan()
        self._scheduler_wake.set()
        self.runtime_logs.add(
            category="media-probe",
            level="success",
            message="每日缺失媒体信息扫描设置已保存",
            dailyEnabled=self.config["dailyEnabled"],
            scanTime=self.config["scanTime"],
            timezone=self.settings.app_timezone,
            nextScanAt=self._iso(self._next_daily_scan_at),
        )
        return await self.status()

    async def trigger_scan(self) -> dict[str, Any]:
        if not self.settings.media_probe_enabled:
            raise AppError(409, "MEDIA_PROBE_ENABLED 已关闭")
        if self._scan_task and not self._scan_task.done():
            raise AppError(409, "缺失媒体信息扫描正在进行中")
        self._scan_task = asyncio.create_task(
            self._scan_missing_media(), name="emby-missing-media-info-scan"
        )
        self._track(self._scan_task)
        return {"accepted": True, "startedAt": datetime.now(UTC).isoformat()}

    def schedule(self, paths: list[str] | tuple[str, ...] | None) -> int:
        candidates = list(
            dict.fromkeys(
                normalize_virtual_path(str(path))
                for path in paths or []
                if str(path).casefold().endswith(".strm")
            )
        )
        candidates = [path for path in candidates if path not in self._active]
        candidates.sort(key=self._episode_sort_key, reverse=True)
        if not candidates or not self.settings.media_probe_enabled or self._closed:
            return 0
        self._active.update(candidates)
        task = asyncio.create_task(
            self._enqueue_many(candidates), name="emby-media-info-enqueue-published"
        )
        self._track(task)
        self.runtime_logs.add(
            category="media-probe",
            level="info",
            message=f"已将 {len(candidates)} 个新媒体提交到 Emby 信息提取队列",
            queuedCount=len(candidates),
        )
        return len(candidates)

    async def _enqueue_many(self, paths: list[str]) -> None:
        for path in paths:
            try:
                await self.repository.enqueue(path)
                self._schedule_put(path, self.settings.media_probe_delay_seconds)
            except Exception as exc:  # noqa: BLE001 - scheduling remains fire-and-forget
                self._active.discard(path)
                self.runtime_logs.add(
                    category="media-probe",
                    level="error",
                    message=f"Emby 媒体信息任务入队失败：{str(exc)[:240]}",
                    targetPath=path,
                    errorType=type(exc).__name__,
                )

    async def _put(self, path: str) -> None:
        if path in self._queued or self._closed:
            return
        self._queued.add(path)
        await self._queue.put(path)

    def _schedule_put(self, path: str, delay: float) -> None:
        async def later() -> None:
            if delay:
                await asyncio.sleep(delay)
            await self._put(path)

        task = asyncio.create_task(later(), name="emby-media-info-wait")
        self._track(task)

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _worker_loop(self) -> None:
        while True:
            path = await self._queue.get()
            self._queued.discard(path)
            try:
                await self._run_one(path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self._active.discard(path)
                self.runtime_logs.add(
                    category="media-probe",
                    level="error",
                    message=f"Emby 媒体信息工作器异常：{str(exc)[:240]}",
                    targetPath=path,
                    errorType=type(exc).__name__,
                )
            finally:
                self._queue.task_done()

    async def _daily_scheduler_loop(self) -> None:
        while True:
            try:
                if not self.config["dailyEnabled"]:
                    self._scheduler_wake.clear()
                    await self._scheduler_wake.wait()
                    continue
                due = self._next_daily_scan_at or self._calculate_next_daily_scan()
                self._next_daily_scan_at = due
                if due is None:
                    await asyncio.sleep(60)
                    continue
                delay = max(0.0, (due - datetime.now(UTC)).total_seconds())
                self._scheduler_wake.clear()
                try:
                    await asyncio.wait_for(self._scheduler_wake.wait(), timeout=delay)
                    continue
                except TimeoutError:
                    pass
                self._next_daily_scan_at = self._calculate_next_daily_scan(
                    datetime.now(UTC) + timedelta(seconds=1)
                )
                try:
                    await self.trigger_scan()
                except AppError as exc:
                    self.runtime_logs.add(
                        category="media-probe",
                        level="warning",
                        message=f"每日缺失媒体信息扫描未启动：{exc.message}",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep daily scheduling alive
                self.runtime_logs.add(
                    category="media-probe",
                    level="error",
                    message=f"每日媒体信息调度器异常：{str(exc)[:240]}",
                    errorType=type(exc).__name__,
                )
                await asyncio.sleep(30)

    async def _scan_missing_media(self) -> None:
        started = datetime.now(UTC)
        self.runtime_logs.add(
            category="media-probe",
            level="info",
            message="开始扫描 Emby 中缺失媒体信息的 STRM",
            targetRoot=self.path_config.emby_strm_root,
        )
        try:
            sources = await self.emby.list_strm_media_sources(
                self.path_config.emby_strm_root,
                timeout=max(30, self.settings.media_probe_timeout),
            )
            missing = [source for source in sources if not source["hasMediaInfo"]]
            queued = self.schedule([str(source["path"]) for source in missing])
            self.config.update(
                {
                    "lastScanAt": datetime.now(UTC).isoformat(),
                    "lastScannedCount": len(sources),
                    "lastMissingCount": len(missing),
                    "lastQueuedCount": queued,
                    "lastError": "",
                }
            )
            await self.runtime_repository.save_media_probe(self.config)
            self.runtime_logs.add(
                category="media-probe",
                level="success",
                message=(f"Emby 缺失媒体信息扫描完成：发现 {len(missing)} 个，已入队 {queued} 个"),
                scannedCount=len(sources),
                missingCount=len(missing),
                queuedCount=queued,
                durationSeconds=round((datetime.now(UTC) - started).total_seconds(), 2),
                targetRoot=self.path_config.emby_strm_root,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report background scan failure
            self.config.update(
                {
                    "lastScanAt": datetime.now(UTC).isoformat(),
                    "lastError": str(exc)[:500],
                }
            )
            await self.runtime_repository.save_media_probe(self.config)
            self.runtime_logs.add(
                category="media-probe",
                level="error",
                message=f"Emby 缺失媒体信息扫描失败：{str(exc)[:300]}",
                errorType=type(exc).__name__,
                targetRoot=self.path_config.emby_strm_root,
            )

    def _calculate_next_daily_scan(self, now: datetime | None = None) -> datetime | None:
        if not self.config["dailyEnabled"]:
            return None
        timezone = self._timezone()
        current = (now or datetime.now(UTC)).astimezone(timezone)
        hour, minute = map(int, str(self.config["scanTime"]).split(":"))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def _timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.app_timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def _default_config(self) -> dict[str, Any]:
        return {
            "dailyEnabled": False,
            "scanTime": "03:00",
            "lastScanAt": None,
            "lastScannedCount": 0,
            "lastMissingCount": 0,
            "lastQueuedCount": 0,
            "lastError": "",
        }

    def _normalize_config(
        self, value: dict[str, Any], *, keep_runtime: bool = True
    ) -> dict[str, Any]:
        result = self._default_config()
        scan_time = str(value.get("scanTime") or "03:00").strip()
        try:
            hour, minute = map(int, scan_time.split(":"))
        except (TypeError, ValueError):
            hour, minute = 3, 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            hour, minute = 3, 0
        result.update(
            {
                "dailyEnabled": bool(value.get("dailyEnabled")),
                "scanTime": f"{hour:02d}:{minute:02d}",
            }
        )
        if keep_runtime:
            for key in (
                "lastScanAt",
                "lastScannedCount",
                "lastMissingCount",
                "lastQueuedCount",
                "lastError",
            ):
                if key in value:
                    result[key] = value[key]
        return result

    async def _run_one(self, target_path: str) -> None:
        attempts = await self.repository.mark_running(target_path)
        self.runtime_logs.add(
            category="media-probe",
            level="info",
            message=f"开始由 Emby 提取媒体信息：{PurePosixPath(target_path).name}",
            targetPath=target_path,
            attempt=attempts,
        )
        try:
            file_info = await self.openlist.get_file_info(target_path)
            if int(file_info.get("size") or 0) <= 0:
                raise EmptyStrmError("目标 STRM 为空，请重新扫描并同步该媒体")
            item = await self.emby.find_item_by_path(target_path)
            if not item or not item.get("Id"):
                raise RuntimeError("Emby 尚未识别该 STRM，等待媒体库扫描")
            item_id = str(item["Id"])
            matching_source = next(
                (
                    source
                    for source in item.get("MediaSources") or []
                    if isinstance(source, dict) and str(source.get("Path") or "") == target_path
                ),
                {},
            )
            media_source_id = str(matching_source.get("Id") or "")
            already_present = self.emby.has_media_info(item, media_source_id)
            if already_present:
                summary = self.emby.media_info_summary(item, media_source_id)
            else:
                await self.emby.extract_media_info(
                    item_id,
                    media_source_id=media_source_id,
                    timeout=self.settings.media_probe_timeout,
                )
                # PlaybackInfo may contain transient probe data even when Emby
                # ultimately did not persist it.  Read the item back and only
                # complete the queue record after the native library metadata is
                # actually available; a delayed write will be picked up by retry.
                refreshed = await self.emby.find_item_by_path(target_path)
                summary = self.emby.media_info_summary(refreshed or {}, media_source_id)
            if not summary["available"]:
                raise RuntimeError("Emby 已响应，但尚未生成有效媒体信息")
            await self.repository.complete(target_path, item_id=item_id)
            self._active.discard(target_path)
            self.runtime_logs.add(
                category="media-probe",
                level="success",
                message=(
                    f"Emby 媒体信息已存在：{PurePosixPath(target_path).name}"
                    if already_present
                    else f"Emby 媒体信息提取完成：{PurePosixPath(target_path).name}"
                ),
                targetPath=target_path,
                embyItemId=item_id,
                container=summary.get("container"),
                videoCodec=summary.get("videoCodec"),
                resolution=summary.get("resolution"),
                durationSeconds=summary.get("durationSeconds"),
                sizeBytes=summary.get("sizeBytes"),
                persistedBy="emby",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one item must not stop the queue
            retry_index = attempts - 1
            retry_delay = (
                PROBE_RETRY_DELAYS[retry_index]
                if not isinstance(exc, EmptyStrmError) and retry_index < len(PROBE_RETRY_DELAYS)
                else None
            )
            next_at = datetime.now(UTC) + timedelta(seconds=retry_delay) if retry_delay else None
            await self.repository.retry(target_path, str(exc), next_at)
            if retry_delay:
                self._schedule_put(target_path, retry_delay)
            else:
                self._active.discard(target_path)
            self.runtime_logs.add(
                category="media-probe",
                level="warning" if retry_delay or isinstance(exc, EmptyStrmError) else "error",
                message=(
                    f"已跳过空的目标 STRM：{PurePosixPath(target_path).name}；请重新扫描并同步"
                    if isinstance(exc, EmptyStrmError)
                    else (
                        f"Emby 媒体信息提取失败，{retry_delay} 秒后重试：{str(exc)[:240]}"
                        if retry_delay
                        else f"Emby 媒体信息提取失败并已停止重试：{str(exc)[:240]}"
                    )
                ),
                targetPath=target_path,
                attempt=attempts,
                nextAttemptAt=next_at.isoformat() if next_at else None,
                errorType=type(exc).__name__,
            )

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _episode_sort_key(path: str) -> tuple[int, int, str]:
        season, episode = source_season_episode(path)
        return season or 0, episode if episode is not None else -1, path.casefold()
