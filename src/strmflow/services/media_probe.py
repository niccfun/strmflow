from __future__ import annotations

import asyncio
import copy
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.repositories.media_probes import MediaProbeRepository
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.schemas.api import MediaProbeConfigUpdate
from strmflow.services.emby import EmbyClient
from strmflow.services.episode_images import EpisodeImageService
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.utils.episodes import source_season_episode
from strmflow.utils.paths import normalize_virtual_path

PROBE_RETRY_DELAYS = (30, 120, 300)
SCAN_BATCH_HISTORY_LIMIT = 20
SCAN_BATCH_ITEM_LIMIT = 1_000
TERMINAL_BATCH_ITEM_STATUSES = {"complete", "failed", "skipped"}


class EmptyStrmError(RuntimeError):
    pass


class MediaProbeService:
    """Serially maintain Emby media info and missing episode images for STRM items."""

    def __init__(
        self,
        settings: Settings,
        repository: MediaProbeRepository,
        runtime_repository: RuntimeSettingsRepository,
        openlist: OpenListClient,
        emby: EmbyClient,
        path_config: PathConfigService,
        runtime_logs: RuntimeLogStore,
        episode_images: EpisodeImageService | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.runtime_repository = runtime_repository
        self.openlist = openlist
        self.emby = emby
        self.path_config = path_config
        self.runtime_logs = runtime_logs
        self.episode_images = episode_images
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
        self._history_lock = asyncio.Lock()
        self._path_batches: dict[str, set[str]] = {}
        self._media_libraries: list[dict[str, Any]] = []
        self._media_libraries_error = ""
        self._media_libraries_loaded_at = 0.0
        self._media_libraries_lock = asyncio.Lock()
        self._closed = False

    async def initialize(self) -> None:
        stored = await self.runtime_repository.load_media_probe()
        if stored:
            self.config = self._normalize_config(stored)
        for batch in self.config.get("scanBatches") or []:
            batch_id = str(batch.get("id") or "")
            for item in batch.get("items") or []:
                if item.get("status") not in TERMINAL_BATCH_ITEM_STATUSES:
                    path = str(item.get("targetPath") or "")
                    if batch_id and path:
                        self._path_batches.setdefault(path, set()).add(batch_id)
        self._apply_runtime_settings()
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
        self._worker = asyncio.create_task(
            self._worker_loop(), name="emby-media-enhancement-worker"
        )
        self._next_daily_scan_at = self._calculate_next_daily_scan()
        self._scheduler = asyncio.create_task(
            self._daily_scheduler_loop(), name="emby-media-enhancement-daily-scheduler"
        )
        for path, delay in sorted(
            pending, key=lambda item: self._episode_sort_key(item[0]), reverse=True
        ):
            self._schedule_put(path, delay)
        self.runtime_logs.add(
            category="media-probe",
            level="success",
            message="Emby 媒体增强队列已启动",
            resumedTaskCount=len(pending),
            delaySeconds=self.settings.media_probe_delay_seconds,
            scanConcurrency=self.settings.media_probe_scan_concurrency,
            queueConcurrency=1,
            dailyEnabled=self.config["dailyEnabled"],
            dailyScanTime=self.config["scanTime"],
            episodeImageEnabled=self.config["episodeImageEnabled"],
            episodeImageSeekPercent=self.config["episodeImageSeekPercent"],
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
            category="media-probe", level="success", message="Emby 媒体增强队列已停止"
        )

    async def status(self) -> dict[str, Any]:
        counts = await self.repository.status_counts()
        libraries = await self._load_media_libraries()
        return {
            "config": {
                "dailyEnabled": bool(self.config["dailyEnabled"]),
                "scanTime": str(self.config["scanTime"]),
                "episodeImageEnabled": bool(self.config["episodeImageEnabled"]),
                "episodeImageLibraryIds": list(self.config["episodeImageLibraryIds"]),
                "timezone": self.settings.app_timezone,
                "scanConcurrency": int(self.config["scanConcurrency"]),
                "probeDelaySeconds": int(self.config["probeDelaySeconds"]),
                "probeTimeoutSeconds": int(self.config["probeTimeoutSeconds"]),
                "episodeImageTimeoutSeconds": int(self.config["episodeImageTimeoutSeconds"]),
                "episodeImageSeekPercent": int(self.config["episodeImageSeekPercent"]),
                "episodeImageMaxWidth": int(self.config["episodeImageMaxWidth"]),
                "episodeImageJpegQuality": int(self.config["episodeImageJpegQuality"]),
            },
            "runtime": {
                "schedulerRunning": bool(self._scheduler and not self._scheduler.done()),
                "scanning": bool(self._scan_task and not self._scan_task.done()),
                "nextScanAt": self._iso(self._next_daily_scan_at),
                "lastScanAt": self.config.get("lastScanAt"),
                "lastScannedCount": int(self.config.get("lastScannedCount") or 0),
                "lastMissingCount": int(self.config.get("lastMissingCount") or 0),
                "lastMissingMediaInfoCount": int(self.config.get("lastMissingMediaInfoCount") or 0),
                "lastMissingImageCount": int(self.config.get("lastMissingImageCount") or 0),
                "lastQueuedCount": int(self.config.get("lastQueuedCount") or 0),
                "lastError": str(self.config.get("lastError") or ""),
                "queueCounts": counts,
                "scanBatches": copy.deepcopy(self.config.get("scanBatches") or []),
                "mediaLibraries": copy.deepcopy(libraries),
                "mediaLibrariesError": str(getattr(self, "_media_libraries_error", "")),
            },
        }

    async def refresh_media_libraries(self) -> dict[str, Any]:
        await self._load_media_libraries(force=True)
        return await self.status()

    async def update_config(self, value: MediaProbeConfigUpdate) -> dict[str, Any]:
        if value.daily_enabled and not self.settings.media_probe_enabled:
            raise AppError(409, "MEDIA_PROBE_ENABLED 已关闭，无法启用每日扫描")
        payload = {
            **self.config,
            **value.model_dump(by_alias=True, exclude_none=True),
        }
        normalized = self._normalize_config(payload, keep_runtime=False)
        self.config.update(
            {
                "dailyEnabled": normalized["dailyEnabled"],
                "scanTime": normalized["scanTime"],
                "episodeImageEnabled": normalized["episodeImageEnabled"],
                "episodeImageLibraryIds": normalized["episodeImageLibraryIds"],
                "scanConcurrency": normalized["scanConcurrency"],
                "probeDelaySeconds": normalized["probeDelaySeconds"],
                "probeTimeoutSeconds": normalized["probeTimeoutSeconds"],
                "episodeImageTimeoutSeconds": normalized["episodeImageTimeoutSeconds"],
                "episodeImageSeekPercent": normalized["episodeImageSeekPercent"],
                "episodeImageMaxWidth": normalized["episodeImageMaxWidth"],
                "episodeImageJpegQuality": normalized["episodeImageJpegQuality"],
            }
        )
        self._apply_runtime_settings()
        await self._persist_config()
        self._next_daily_scan_at = self._calculate_next_daily_scan()
        self._scheduler_wake.set()
        self.runtime_logs.add(
            category="media-probe",
            level="success",
            message="每日媒体增强扫描设置已保存",
            dailyEnabled=self.config["dailyEnabled"],
            scanTime=self.config["scanTime"],
            episodeImageEnabled=self.config["episodeImageEnabled"],
            episodeImageLibraryCount=len(self.config["episodeImageLibraryIds"]),
            episodeImageSeekPercent=self.config["episodeImageSeekPercent"],
            episodeImageMaxWidth=self.config["episodeImageMaxWidth"],
            scanConcurrency=self.config["scanConcurrency"],
            timezone=self.settings.app_timezone,
            nextScanAt=self._iso(self._next_daily_scan_at),
        )
        return await self.status()

    async def trigger_scan(self, trigger: str = "manual") -> dict[str, Any]:
        if not self.settings.media_probe_enabled:
            raise AppError(409, "MEDIA_PROBE_ENABLED 已关闭")
        if self._scan_task and not self._scan_task.done():
            raise AppError(409, "媒体增强扫描正在进行中")
        batch_id = secrets.token_hex(6)
        self._scan_task = asyncio.create_task(
            self._scan_missing_media(trigger=trigger, batch_id=batch_id),
            name="emby-missing-media-enhancement-scan",
        )
        self._track(self._scan_task)
        return {
            "accepted": True,
            "batchId": batch_id,
            "startedAt": datetime.now(UTC).isoformat(),
        }

    def schedule(
        self,
        paths: list[str] | tuple[str, ...] | None,
        *,
        batch_id: str = "",
    ) -> int:
        all_candidates = list(
            dict.fromkeys(
                normalize_virtual_path(str(path))
                for path in paths or []
                if str(path).casefold().endswith(".strm")
            )
        )
        if batch_id:
            if not hasattr(self, "_path_batches"):
                self._path_batches = {}
            for path in all_candidates:
                self._path_batches.setdefault(path, set()).add(batch_id)
        candidates = [path for path in all_candidates if path not in self._active]
        candidates.sort(key=self._episode_sort_key, reverse=True)
        if not candidates or not self.settings.media_probe_enabled or self._closed:
            return 0
        self._active.update(candidates)
        task = asyncio.create_task(
            self._enqueue_many(candidates), name="emby-media-enhancement-enqueue-published"
        )
        self._track(task)
        self.runtime_logs.add(
            category="media-probe",
            level="info",
            message=f"已将 {len(candidates)} 个媒体提交到 Emby 增强队列",
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
                await self._update_scan_batch_item(
                    path,
                    status="failed",
                    level="error",
                    message=f"任务入队失败：{str(exc)[:240]}",
                )
                self.runtime_logs.add(
                    category="media-probe",
                    level="error",
                    message=f"Emby 媒体增强任务入队失败：{str(exc)[:240]}",
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

        task = asyncio.create_task(later(), name="emby-media-enhancement-wait")
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
                await self._update_scan_batch_item(
                    path,
                    status="failed",
                    level="error",
                    message=f"工作器异常：{str(exc)[:240]}",
                )
                self.runtime_logs.add(
                    category="media-probe",
                    level="error",
                    message=f"Emby 媒体增强工作器异常：{str(exc)[:240]}",
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
                    await self.trigger_scan("scheduled")
                except AppError as exc:
                    self.runtime_logs.add(
                        category="media-probe",
                        level="warning",
                        message=f"每日媒体增强扫描未启动：{exc.message}",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep daily scheduling alive
                self.runtime_logs.add(
                    category="media-probe",
                    level="error",
                    message=f"每日媒体增强调度器异常：{str(exc)[:240]}",
                    errorType=type(exc).__name__,
                )
                await asyncio.sleep(30)

    async def _scan_missing_media(self, trigger: str = "manual", batch_id: str = "") -> None:
        started = datetime.now(UTC)
        batch_id = batch_id or secrets.token_hex(6)
        batch = {
            "id": batch_id,
            "trigger": "scheduled" if trigger == "scheduled" else "manual",
            "status": "scanning",
            "startedAt": started.isoformat(),
            "scanFinishedAt": None,
            "finishedAt": None,
            "scannedCount": 0,
            "missingMediaInfoCount": 0,
            "missingImageCount": 0,
            "queuedCount": 0,
            "completeCount": 0,
            "failedCount": 0,
            "omittedItemCount": 0,
            "omittedPendingCount": 0,
            "omittedCompleteCount": 0,
            "omittedFailedCount": 0,
            "durationSeconds": 0,
            "error": "",
            "items": [],
        }
        self.config["scanBatches"] = [
            batch,
            *[
                value
                for value in self.config.get("scanBatches") or []
                if str(value.get("id") or "") != batch_id
            ],
        ][:SCAN_BATCH_HISTORY_LIMIT]
        await self._persist_config()
        self.runtime_logs.add(
            category="media-probe",
            level="info",
            message="开始扫描 Emby 中缺失媒体信息或集图片的 STRM",
            targetRoot=self.path_config.emby_strm_root,
            scanConcurrency=self.settings.media_probe_scan_concurrency,
            queueConcurrency=1,
            batchId=batch_id,
        )
        try:
            sources = await self.emby.list_strm_media_sources(
                self.path_config.emby_strm_root,
                timeout=max(30, self.settings.media_probe_timeout),
            )
            selected_image_libraries = set(self.config.get("episodeImageLibraryIds") or [])
            image_libraries = await self._load_media_libraries() if selected_image_libraries else []

            def image_library_selected(source: dict[str, Any]) -> bool:
                if not selected_image_libraries:
                    return True
                library = self.emby.media_library_for_path(
                    str(source.get("path") or ""), image_libraries
                )
                return bool(library and str(library.get("id") or "") in selected_image_libraries)

            missing_media_info = [source for source in sources if not source["hasMediaInfo"]]
            missing_images = [
                source
                for source in sources
                if self.config["episodeImageEnabled"]
                and str(source.get("itemType") or "").casefold() == "episode"
                and not source.get("hasPrimaryImage")
                and image_library_selected(source)
            ]
            missing_image_paths = {str(source["path"]) for source in missing_images}
            missing_by_path = {
                str(source["path"]): source for source in [*missing_media_info, *missing_images]
            }
            active_before = set(getattr(self, "_active", set()))
            detailed_sources = list(missing_by_path.values())[:SCAN_BATCH_ITEM_LIMIT]
            batch["items"] = [
                {
                    "targetPath": str(source["path"]),
                    "fileName": PurePosixPath(str(source["path"])).name,
                    "itemId": str(source.get("itemId") or ""),
                    "missingMediaInfo": not bool(source.get("hasMediaInfo")),
                    "missingImage": (str(source["path"]) in missing_image_paths),
                    "status": "waiting" if str(source["path"]) in active_before else "queued",
                    "attempts": 0,
                    "message": (
                        "已关联正在执行的任务"
                        if str(source["path"]) in active_before
                        else "已加入单线程增强队列"
                    ),
                    "updatedAt": datetime.now(UTC).isoformat(),
                    "logs": [
                        {
                            "time": datetime.now(UTC).isoformat(),
                            "level": "info",
                            "message": (
                                "检测到媒体信息和集图片缺失"
                                if not source.get("hasMediaInfo") and source in missing_images
                                else (
                                    "检测到媒体信息缺失"
                                    if not source.get("hasMediaInfo")
                                    else "检测到集图片缺失"
                                )
                            ),
                        }
                    ],
                }
                for source in detailed_sources
            ]
            batch["omittedItemCount"] = max(0, len(missing_by_path) - len(detailed_sources))
            batch["omittedPendingCount"] = batch["omittedItemCount"]
            queued = self.schedule(list(missing_by_path), batch_id=batch_id)
            scan_finished = datetime.now(UTC)
            batch.update(
                {
                    "status": "running" if missing_by_path else "complete",
                    "scanFinishedAt": scan_finished.isoformat(),
                    "finishedAt": None if missing_by_path else scan_finished.isoformat(),
                    "scannedCount": len(sources),
                    "missingMediaInfoCount": len(missing_media_info),
                    "missingImageCount": len(missing_images),
                    "queuedCount": queued,
                    "durationSeconds": round((scan_finished - started).total_seconds(), 2),
                }
            )
            self.config.update(
                {
                    "lastScanAt": datetime.now(UTC).isoformat(),
                    "lastScannedCount": len(sources),
                    "lastMissingCount": len(missing_by_path),
                    "lastMissingMediaInfoCount": len(missing_media_info),
                    "lastMissingImageCount": len(missing_images),
                    "lastQueuedCount": queued,
                    "lastError": "",
                }
            )
            await self._persist_config()
            self.runtime_logs.add(
                category="media-probe",
                level="success",
                message=(
                    "Emby 媒体增强扫描完成："
                    f"媒体信息缺失 {len(missing_media_info)} 个、集图片缺失 {len(missing_images)} 个、"
                    f"去重后入队 {queued} 个"
                ),
                scannedCount=len(sources),
                missingCount=len(missing_by_path),
                missingMediaInfoCount=len(missing_media_info),
                missingImageCount=len(missing_images),
                queuedCount=queued,
                scanConcurrency=self.settings.media_probe_scan_concurrency,
                queueConcurrency=1,
                durationSeconds=round((datetime.now(UTC) - started).total_seconds(), 2),
                targetRoot=self.path_config.emby_strm_root,
                batchId=batch_id,
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
            batch.update(
                {
                    "status": "failed",
                    "scanFinishedAt": datetime.now(UTC).isoformat(),
                    "finishedAt": datetime.now(UTC).isoformat(),
                    "durationSeconds": round((datetime.now(UTC) - started).total_seconds(), 2),
                    "error": str(exc)[:500],
                }
            )
            await self._persist_config()
            self.runtime_logs.add(
                category="media-probe",
                level="error",
                message=f"Emby 媒体增强扫描失败：{str(exc)[:300]}",
                errorType=type(exc).__name__,
                targetRoot=self.path_config.emby_strm_root,
                batchId=batch_id,
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
        timezone = self.settings.app_timezone
        try:
            return ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Shanghai")

    async def _load_media_libraries(self, *, force: bool = False) -> list[dict[str, Any]]:
        loaded_at = float(getattr(self, "_media_libraries_loaded_at", 0.0))
        cached = getattr(self, "_media_libraries", [])
        if not force and loaded_at and monotonic() - loaded_at < 300:
            return list(cached)
        lock = getattr(self, "_media_libraries_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._media_libraries_lock = lock
        async with lock:
            loaded_at = float(getattr(self, "_media_libraries_loaded_at", 0.0))
            if not force and loaded_at and monotonic() - loaded_at < 300:
                return list(getattr(self, "_media_libraries", []))
            try:
                libraries = await self.emby.list_media_libraries(timeout=10)
                target_root = normalize_virtual_path(self.path_config.emby_strm_root)
                relevant: list[dict[str, Any]] = []
                for library in libraries:
                    collection_type = str(library.get("collectionType") or "").casefold()
                    if collection_type not in {"", "tvshows", "mixed"}:
                        continue
                    locations = [
                        normalize_virtual_path(str(location))
                        for location in library.get("locations") or []
                    ]
                    overlaps = any(
                        location == target_root
                        or location.startswith(target_root.rstrip("/") + "/")
                        or target_root.startswith(location.rstrip("/") + "/")
                        for location in locations
                    )
                    if overlaps:
                        relevant.append(dict(library))
                self._media_libraries = relevant
                self._media_libraries_error = ""
                self._media_libraries_loaded_at = monotonic()
            except Exception as exc:  # noqa: BLE001 - settings page remains usable
                self._media_libraries_error = str(exc)[:300]
                self._media_libraries_loaded_at = monotonic()
                self.runtime_logs.add(
                    category="media-probe",
                    level="warning",
                    message=f"读取 Emby 媒体库列表失败：{str(exc)[:240]}",
                )
            return list(getattr(self, "_media_libraries", []))

    async def _episode_image_enabled_for_path(self, target_path: str) -> bool:
        if not self.config["episodeImageEnabled"]:
            return False
        selected = set(self.config.get("episodeImageLibraryIds") or [])
        if not selected:
            return True
        libraries = await self._load_media_libraries()
        library = self.emby.media_library_for_path(target_path, libraries)
        return bool(library and str(library.get("id") or "") in selected)

    def _default_config(self) -> dict[str, Any]:
        return {
            "dailyEnabled": False,
            "scanTime": "03:00",
            "episodeImageEnabled": self.settings.episode_image_enabled,
            "episodeImageLibraryIds": [],
            "scanConcurrency": self.settings.media_probe_scan_concurrency,
            "probeDelaySeconds": self.settings.media_probe_delay_seconds,
            "probeTimeoutSeconds": self.settings.media_probe_timeout,
            "episodeImageTimeoutSeconds": self.settings.episode_image_timeout,
            "episodeImageSeekPercent": self.settings.episode_image_seek_percent,
            "episodeImageMaxWidth": self.settings.episode_image_max_width,
            "episodeImageJpegQuality": self.settings.episode_image_jpeg_quality,
            "lastScanAt": None,
            "lastScannedCount": 0,
            "lastMissingCount": 0,
            "lastMissingMediaInfoCount": 0,
            "lastMissingImageCount": 0,
            "lastQueuedCount": 0,
            "lastError": "",
            "scanBatches": [],
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
                "episodeImageEnabled": bool(
                    value.get("episodeImageEnabled", self.settings.episode_image_enabled)
                ),
                "episodeImageLibraryIds": list(
                    dict.fromkeys(
                        str(library_id).strip()
                        for library_id in value.get("episodeImageLibraryIds") or []
                        if str(library_id).strip()
                    )
                )[:100],
                "scanConcurrency": max(
                    1,
                    min(
                        32,
                        int(
                            value.get("scanConcurrency", self.settings.media_probe_scan_concurrency)
                        ),
                    ),
                ),
                "probeDelaySeconds": max(
                    0,
                    min(
                        300,
                        int(
                            value.get("probeDelaySeconds", self.settings.media_probe_delay_seconds)
                        ),
                    ),
                ),
                "probeTimeoutSeconds": max(
                    10,
                    min(
                        600,
                        int(value.get("probeTimeoutSeconds", self.settings.media_probe_timeout)),
                    ),
                ),
                "episodeImageTimeoutSeconds": max(
                    10,
                    min(
                        600,
                        int(
                            value.get(
                                "episodeImageTimeoutSeconds",
                                self.settings.episode_image_timeout,
                            )
                        ),
                    ),
                ),
                "episodeImageSeekPercent": max(
                    5,
                    min(
                        90,
                        int(
                            value.get(
                                "episodeImageSeekPercent",
                                self.settings.episode_image_seek_percent,
                            )
                        ),
                    ),
                ),
                "episodeImageMaxWidth": max(
                    320,
                    min(
                        3840,
                        int(
                            value.get(
                                "episodeImageMaxWidth",
                                self.settings.episode_image_max_width,
                            )
                        ),
                    ),
                ),
                "episodeImageJpegQuality": max(
                    1,
                    min(
                        10,
                        int(
                            value.get(
                                "episodeImageJpegQuality",
                                self.settings.episode_image_jpeg_quality,
                            )
                        ),
                    ),
                ),
            }
        )
        if keep_runtime:
            for key in (
                "lastScanAt",
                "lastScannedCount",
                "lastMissingCount",
                "lastMissingMediaInfoCount",
                "lastMissingImageCount",
                "lastQueuedCount",
                "lastError",
                "scanBatches",
            ):
                if key in value:
                    result[key] = (
                        self._normalize_scan_batches(value[key])
                        if key == "scanBatches"
                        else value[key]
                    )
        return result

    def _apply_runtime_settings(self) -> None:
        """Apply SQLite-backed operational settings to all shared service clients."""
        self.settings.media_probe_scan_concurrency = int(self.config["scanConcurrency"])
        self.settings.media_probe_delay_seconds = int(self.config["probeDelaySeconds"])
        self.settings.media_probe_timeout = int(self.config["probeTimeoutSeconds"])
        self.settings.episode_image_timeout = int(self.config["episodeImageTimeoutSeconds"])
        self.settings.episode_image_seek_percent = int(self.config["episodeImageSeekPercent"])
        self.settings.episode_image_max_width = int(self.config["episodeImageMaxWidth"])
        self.settings.episode_image_jpeg_quality = int(self.config["episodeImageJpegQuality"])

    async def _persist_config(self) -> None:
        lock = getattr(self, "_history_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._history_lock = lock
        async with lock:
            await self.runtime_repository.save_media_probe(self.config)

    @staticmethod
    def _normalize_scan_batches(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        batches: list[dict[str, Any]] = []
        for raw_batch in value[:SCAN_BATCH_HISTORY_LIMIT]:
            if not isinstance(raw_batch, dict) or not raw_batch.get("id"):
                continue
            batch = copy.deepcopy(raw_batch)
            raw_items = batch.get("items")
            items: list[dict[str, Any]] = []
            if isinstance(raw_items, list):
                for raw_item in raw_items[:SCAN_BATCH_ITEM_LIMIT]:
                    if not isinstance(raw_item, dict) or not raw_item.get("targetPath"):
                        continue
                    item = dict(raw_item)
                    logs = item.get("logs")
                    item["logs"] = (
                        [dict(entry) for entry in logs[-12:] if isinstance(entry, dict)]
                        if isinstance(logs, list)
                        else []
                    )
                    items.append(item)
            batch["items"] = items
            batches.append(batch)
        return batches

    async def _update_scan_batch_item(
        self,
        target_path: str,
        *,
        status: str,
        level: str,
        message: str,
        attempts: int | None = None,
        item_id: str = "",
    ) -> None:
        path_batches = getattr(self, "_path_batches", None)
        if not path_batches:
            return
        batch_ids = set(path_batches.get(target_path) or set())
        if not batch_ids:
            return

        now = datetime.now(UTC).isoformat()
        terminal = status in TERMINAL_BATCH_ITEM_STATUSES
        changed = False
        for batch in self.config.get("scanBatches") or []:
            if str(batch.get("id") or "") not in batch_ids:
                continue
            matched = False
            for item in batch.get("items") or []:
                if str(item.get("targetPath") or "") != target_path:
                    continue
                matched = True
                item["status"] = status
                item["message"] = message
                item["updatedAt"] = now
                if attempts is not None:
                    item["attempts"] = attempts
                if item_id:
                    item["itemId"] = item_id
                logs = item.setdefault("logs", [])
                logs.append(
                    {
                        "time": now,
                        "level": level,
                        "message": message,
                        **({"attempt": attempts} if attempts is not None else {}),
                    }
                )
                item["logs"] = logs[-12:]
                break

            if terminal and not matched:
                batch["omittedPendingCount"] = max(
                    0, int(batch.get("omittedPendingCount") or 0) - 1
                )
                key = "omittedCompleteCount" if status == "complete" else "omittedFailedCount"
                batch[key] = int(batch.get(key) or 0) + 1

            items = batch.get("items") or []
            batch["completeCount"] = sum(item.get("status") == "complete" for item in items) + int(
                batch.get("omittedCompleteCount") or 0
            )
            batch["failedCount"] = sum(
                item.get("status") in {"failed", "skipped"} for item in items
            ) + int(batch.get("omittedFailedCount") or 0)
            pending = (
                any(item.get("status") not in TERMINAL_BATCH_ITEM_STATUSES for item in items)
                or int(batch.get("omittedPendingCount") or 0) > 0
            )
            if not pending and batch.get("status") != "failed":
                batch["status"] = (
                    "complete_with_errors" if int(batch["failedCount"]) else "complete"
                )
                batch["finishedAt"] = now
            elif batch.get("status") not in {"failed", "scanning"}:
                batch["status"] = "running"
            changed = True

        if terminal:
            remaining = path_batches.get(target_path)
            if remaining:
                remaining.difference_update(batch_ids)
                if not remaining:
                    path_batches.pop(target_path, None)
        if changed:
            await self._persist_config()

    async def _run_one(self, target_path: str) -> None:
        attempts = await self.repository.mark_running(target_path)
        await self._update_scan_batch_item(
            target_path,
            status="running",
            level="info",
            message="开始读取媒体并执行增强",
            attempts=attempts,
        )
        self.runtime_logs.add(
            category="media-probe",
            level="info",
            message=f"开始处理 Emby 媒体增强：{PurePosixPath(target_path).name}",
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
            refreshed = item
            summary = self.emby.media_info_summary(item, media_source_id)
            persistence_confirmed = already_present
            media_info_error: Exception | None = None
            if not already_present:
                try:
                    probe_payload = await self.emby.extract_media_info(
                        item_id,
                        media_source_id=media_source_id,
                        timeout=self.settings.media_probe_timeout,
                    )
                    probe_summary = self.emby.media_info_summary(probe_payload, media_source_id)
                    # PlaybackInfo returns the freshly probed data before Emby's item
                    # query cache necessarily exposes the asynchronous database write.
                    # Prefer a read-back confirmation, but treat a valid native
                    # PlaybackInfo result as success instead of logging a false failure.
                    refreshed = await self.emby.find_item_by_path(target_path)
                    persisted_summary = self.emby.media_info_summary(
                        refreshed or {}, media_source_id
                    )
                    persistence_confirmed = bool(persisted_summary["available"])
                    summary = persisted_summary if persistence_confirmed else probe_summary
                    if not summary["available"]:
                        raise RuntimeError(
                            "Emby PlaybackInfo 未返回有效媒体信息；"
                            "请检查 Emby 是否能访问 STRM 中的媒体地址"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - image enhancement is independent
                    media_info_error = exc
            image_status = "disabled"
            image_bytes = 0
            image_error: Exception | None = None
            try:
                if (
                    str((refreshed or item).get("Type") or "").casefold() == "episode"
                    and await self._episode_image_enabled_for_path(target_path)
                    and self.episode_images
                ):
                    image_result = await self.episode_images.ensure_primary_image(
                        refreshed or item,
                        target_path,
                        duration_seconds=float(summary.get("durationSeconds") or 0),
                    )
                    image_status = image_result.status
                    image_bytes = image_result.image_bytes
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report both enhancement steps together
                image_error = exc
            if media_info_error and image_error:
                raise RuntimeError(
                    f"媒体信息：{media_info_error}；剧集图片：{image_error}"
                ) from image_error
            if media_info_error:
                raise media_info_error
            if image_error:
                raise image_error
            await self.repository.complete(target_path, item_id=item_id)
            self._active.discard(target_path)
            batch_message = "媒体信息已存在" if already_present else "媒体信息提取完成"
            if image_status == "created":
                batch_message += "，集图片已生成并上传"
            elif image_status == "exists":
                batch_message += "，集图片已存在"
            await self._update_scan_batch_item(
                target_path,
                status="complete",
                level="success",
                message=batch_message,
                attempts=attempts,
                item_id=item_id,
            )
            self.runtime_logs.add(
                category="media-probe",
                level="success",
                message=(
                    f"Emby 媒体信息已存在：{PurePosixPath(target_path).name}"
                    if already_present
                    else (
                        f"Emby 媒体信息提取完成：{PurePosixPath(target_path).name}"
                        if persistence_confirmed
                        else (
                            "Emby 媒体信息提取完成，条目缓存刷新中："
                            f"{PurePosixPath(target_path).name}"
                        )
                    )
                ),
                targetPath=target_path,
                embyItemId=item_id,
                container=summary.get("container"),
                videoCodec=summary.get("videoCodec"),
                resolution=summary.get("resolution"),
                durationSeconds=summary.get("durationSeconds"),
                sizeBytes=summary.get("sizeBytes"),
                persistedBy="emby",
                persistenceConfirmed=persistence_confirmed,
                mediaInfoSource=(
                    "item" if already_present or persistence_confirmed else "playback-info"
                ),
                episodeImageStatus=image_status,
                episodeImageBytes=image_bytes,
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
            batch_status = "retry" if retry_delay else "failed"
            batch_message = (
                f"处理失败，{retry_delay} 秒后重试：{str(exc)[:200]}"
                if retry_delay
                else f"处理失败并停止重试：{str(exc)[:200]}"
            )
            await self._update_scan_batch_item(
                target_path,
                status=batch_status,
                level="warning" if retry_delay else "error",
                message=batch_message,
                attempts=attempts,
            )
            self.runtime_logs.add(
                category="media-probe",
                level="warning" if retry_delay or isinstance(exc, EmptyStrmError) else "error",
                message=(
                    f"已跳过空的目标 STRM：{PurePosixPath(target_path).name}；请重新扫描并同步"
                    if isinstance(exc, EmptyStrmError)
                    else (
                        f"Emby 媒体增强失败，{retry_delay} 秒后重试：{str(exc)[:240]}"
                        if retry_delay
                        else f"Emby 媒体增强失败并已停止重试：{str(exc)[:240]}"
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
