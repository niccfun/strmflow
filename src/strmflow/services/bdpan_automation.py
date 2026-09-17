from __future__ import annotations

import asyncio
import hashlib
import posixpath
import random
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from time import perf_counter
from typing import TYPE_CHECKING, Any

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.media_layout import (
    media_resource_path,
    media_type_for_type,
    normalize_category,
)
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.schemas.api import (
    BdpanAutomationConfigUpdate,
    BdpanShareImportRequest,
    MediaItemInput,
    PublishRequest,
)
from strmflow.services.bdpan import BdpanCli, BdpanCliError
from strmflow.services.emby import EmbyClient
from strmflow.services.media import MediaService
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.utils.episodes import (
    media_quality_rank,
    select_preferred_episodes,
    source_season_episode,
)
from strmflow.utils.paths import join_virtual_path, relative_virtual_path, validate_folder_name

if TYPE_CHECKING:
    from strmflow.services.emby302 import Emby302Gateway
    from strmflow.services.media_probe import MediaProbeService
    from strmflow.services.notifications import WecomWebhookService
    from strmflow.services.storage import StorageService

SOURCE_VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".rmvb",
    ".ts",
    ".webm",
    ".wmv",
}
SEASON_DIRECTORY = re.compile(
    r"(?i)^\s*(?:season[ ._-]*0*\d{1,2}|s0*\d{1,2}(?:\b|[ ._-])|第\s*0*\d{1,2}\s*季)"
)


@dataclass(frozen=True, slots=True)
class ShareMediaFile:
    fsid: str
    name: str
    relative_parts: tuple[str, ...]
    size: int
    modified: str

    @property
    def fingerprint(self) -> str:
        identity = "/".join(self.relative_parts).casefold()
        return hashlib.sha256(f"{identity}\0{self.size}".encode()).hexdigest()[:24]


class BdpanAutomationService:
    """Poll shared folders, selectively transfer additions, then run STRM sync."""

    def __init__(
        self,
        settings: Settings,
        cli: BdpanCli,
        repository: RuntimeSettingsRepository,
        media: MediaService,
        openlist: OpenListClient,
        emby: EmbyClient,
        path_config: PathConfigService,
        runtime_logs: RuntimeLogStore,
        notifications: WecomWebhookService | None = None,
        emby302: Emby302Gateway | None = None,
        media_probe: MediaProbeService | None = None,
        storage: StorageService | None = None,
    ) -> None:
        self.settings = settings
        self.cli = cli
        self.repository = repository
        self.media = media
        self.openlist = openlist
        self.emby = emby
        self.path_config = path_config
        self.runtime_logs = runtime_logs
        self.notifications = notifications
        self.emby302 = emby302
        self.media_probe = media_probe
        self.storage = storage
        self.config = self._default_config()
        self.states: dict[str, dict[str, Any]] = {}
        self._scheduler: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._operation_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._running_item_id = ""
        self._manual_check_pending = False
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._quota_cache: tuple[float, int, dict[str, Any]] | None = None
        self._share_previews: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        stored_config = await self.repository.load_bdpan()
        if stored_config:
            try:
                self.config = self._normalize_config(stored_config)
            except (AppError, TypeError, ValueError):
                self._log("warning", "已忽略格式不正确的百度网盘运行配置")
        stored_states = await self.repository.load_bdpan_watch_states()
        if stored_states:
            self.states = {
                str(key): dict(value)
                for key, value in stored_states.items()
                if isinstance(value, dict)
            }
        items = await self.media.list_items()
        watched_count = sum(
            item.get("status") == "ongoing" and bool(item.get("baiduLink")) for item in items
        )
        self._scheduler = asyncio.create_task(self._scheduler_loop(), name="bdpan-scheduler")
        self._log(
            "success",
            "百度网盘自动追更调度器已启动",
            enabled=self.config["enabled"],
            intervalMinutes=self.config["checkIntervalMinutes"],
            watchedCount=watched_count,
            savedStateCount=len(self.states),
            saveRoot=self.config["saveRoot"],
        )
        if not self.config["enabled"]:
            self._log("info", "百度网盘定时检查当前未启用，手动检查仍可使用")

    async def close(self) -> None:
        self._log(
            "info",
            "正在停止百度网盘自动追更任务",
            activeTaskCount=len(self._tasks),
            schedulerRunning=bool(self._scheduler and not self._scheduler.done()),
        )
        if self._scheduler:
            self._scheduler.cancel()
        for task in self._tasks:
            task.cancel()
        pending = [task for task in [self._scheduler, *self._tasks] if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._log("success", "百度网盘自动追更任务已停止")

    async def status(self, *, refresh: bool = False) -> dict[str, Any]:
        cli_status = await self._cli_status(refresh=refresh)
        items = [
            item
            for item in await self.media.list_items()
            if item.get("status") == "ongoing" and item.get("baiduLink")
        ]
        watches = [self._public_watch(item) for item in items]
        next_checks = [watch["nextCheckAt"] for watch in watches if watch.get("nextCheckAt")]
        last_checks = [watch["lastCheckedAt"] for watch in watches if watch.get("lastCheckedAt")]
        return {
            "config": dict(self.config),
            "runtime": {
                **cli_status,
                "schedulerRunning": self._scheduler is not None and not self._scheduler.done(),
                "checking": self._operation_lock.locked() or self._manual_check_pending,
                "runningItemId": self._running_item_id,
                "watchedCount": len(items),
                "nextCheckAt": min(next_checks) if next_checks else None,
                "lastCheckedAt": max(last_checks) if last_checks else None,
            },
            "watches": watches,
        }

    async def quota(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return a short-lived account-capacity snapshot from bdpan CLI."""
        loop_time = asyncio.get_running_loop().time()
        selected = self.settings.bdpan_binary
        if (
            not refresh
            and self._quota_cache
            and loop_time - self._quota_cache[0] < self._quota_cache[1]
            and self._quota_cache[2].get("configuredBinary") == selected
        ):
            return dict(self._quota_cache[2])
        try:
            quota = await self.cli.quota(selected)
        except Exception as exc:  # noqa: BLE001 - overview remains available on CLI failure
            quota = {
                "available": False,
                "supported": True,
                "totalBytes": 0,
                "usedBytes": 0,
                "freeBytes": 0,
                "usedPercent": 0,
                "source": "bdpan 配置 · 百度开放 API",
                "error": str(exc)[:300],
            }
        quota["configuredBinary"] = selected
        ttl = 300 if quota.get("available") else 60
        self._quota_cache = (loop_time, ttl, quota)
        return dict(quota)

    async def update_config(self, value: BdpanAutomationConfigUpdate) -> dict[str, Any]:
        config = self._normalize_config(value.model_dump(by_alias=True))
        if config["enabled"]:
            status = await self._cli_status(refresh=True)
            if not status["available"]:
                raise AppError(409, "镜像内未找到 bdpan CLI")
            if not status["loggedIn"]:
                raise AppError(409, "bdpan 尚未完成百度网盘授权")
        self.config = config
        self._status_cache = None
        self._quota_cache = None
        await self.repository.save_bdpan(config)
        if config["enabled"]:
            now = self._iso_now()
            for item in await self.media.list_items():
                if item.get("status") == "ongoing" and item.get("baiduLink"):
                    self.states.setdefault(item["id"], {})["nextCheckAt"] = now
            await self._save_states()
        self._wake.set()
        self._log(
            "success",
            "百度网盘自动追更配置已更新",
            enabled=config["enabled"],
            trackingMode=config["trackingMode"],
            intervalMinutes=config["checkIntervalMinutes"],
            maxNewItems=config["maxNewItems"],
        )
        return await self.status(refresh=True)

    async def start_login(self, accepted: bool) -> dict[str, Any]:
        if not accepted:
            raise AppError(400, "请先阅读并确认百度网盘安全提示")
        try:
            url = await self.cli.start_login(self.settings.bdpan_binary)
        except BdpanCliError as exc:
            raise AppError(502, str(exc)) from exc
        self._log("info", "百度网盘授权链接已生成")
        return {"authorizationUrl": url, "expiresIn": 600}

    async def complete_login(self, code: str) -> dict[str, Any]:
        try:
            result = await self.cli.complete_login(code, self.settings.bdpan_binary)
        except BdpanCliError as exc:
            raise AppError(502, str(exc)) from exc
        self._status_cache = None
        self._quota_cache = None
        self._wake.set()
        self._log("success", "百度网盘授权已完成")
        return result

    async def logout(self) -> dict[str, Any]:
        if self._operation_lock.locked() or self._manual_check_pending:
            raise AppError(409, "百度网盘任务正在运行，请完成后再退出账号")
        try:
            await self.cli.logout(self.settings.bdpan_binary)
        except BdpanCliError as exc:
            raise AppError(502, str(exc)) from exc
        self.config["enabled"] = False
        self._status_cache = None
        self._quota_cache = None
        self._share_previews.clear()
        await self.repository.save_bdpan(self.config)
        self._wake.set()
        self._log("success", "百度网盘账号已退出，自动追更已关闭")
        return await self.status(refresh=True)

    async def inspect_share(self, value: str, extract_code: str = "") -> dict[str, Any]:
        """Inspect a share once and keep its file identifiers only in short-lived memory."""
        cli_status = await self._cli_status()
        if not cli_status["available"]:
            raise AppError(503, "bdpan CLI 未安装或配置路径不正确")
        if not cli_status["loggedIn"]:
            raise AppError(409, "请先在“自动追更”中完成百度网盘授权")
        if self._operation_lock.locked():
            raise AppError(409, "已有百度网盘任务正在运行，请稍后重试")

        share_url, code = self.cli.parse_share_input(value, extract_code)
        self._log("info", "开始读取百度网盘分享内容")
        async with self._operation_lock:
            try:
                files = await self._list_share_media(
                    share_url,
                    code,
                    self.cli.new_session_id(),
                    strip_wrapper=False,
                )
            except BdpanCliError as exc:
                raise AppError(502, str(exc)) from exc
        if not files:
            raise AppError(409, "分享中未发现可保存的原始视频文件")

        candidates = self._share_candidates(files)
        preview_id = secrets.token_urlsafe(24)
        now = asyncio.get_running_loop().time()
        self._prune_share_previews(now)
        self._share_previews[preview_id] = {
            "createdAt": now,
            "shareUrl": share_url,
            "extractCode": code,
            "candidates": candidates,
        }
        public_candidates = [
            {
                key: candidate[key]
                for key in (
                    "id",
                    "name",
                    "title",
                    "year",
                    "fileCount",
                    "duplicateCount",
                    "totalBytes",
                    "sampleFiles",
                )
            }
            for candidate in candidates
        ]
        self._log(
            "info",
            f"百度网盘分享检查完成：发现 {len(files)} 个有效媒体文件",
            candidateCount=len(candidates),
        )
        return {
            "previewId": preview_id,
            "fileCount": len(files),
            "candidateCount": len(candidates),
            "candidates": public_candidates,
            "expiresIn": 900,
        }

    async def import_share(self, body: BdpanShareImportRequest) -> dict[str, Any]:
        """Transfer an inspected share into the selected built-in media directory."""
        preview = self._get_share_preview(body.preview_id)
        candidate = next(
            (
                item
                for item in preview["candidates"]
                if isinstance(item, dict) and item.get("id") == body.candidate_id
            ),
            None,
        )
        if not candidate:
            raise AppError(404, "选择的分享媒体已过期，请重新检查分享链接")
        type_dir = validate_folder_name(body.type_dir)
        category = validate_folder_name(body.category)
        normalized_category = normalize_category(type_dir, category)
        if normalized_category is None:
            raise AppError(400, "保存目录不在系统内置媒体分类中")
        category = normalized_category
        media_type = media_type_for_type(type_dir)
        title = validate_folder_name(body.title)
        year = str(body.year or "").strip()
        if year and not re.fullmatch(r"\d{4}", year):
            raise AppError(400, "年份必须是 4 位数字")
        folder_name = validate_folder_name(f"{title} ({year})" if year else title)
        if not self.path_config.list_root:
            raise AppError(409, "请先设置只读源 STRM 根目录")
        source_path = media_resource_path(
            self.path_config.list_root,
            type_dir,
            category,
            folder_name,
        )
        duplicate = next(
            (
                item
                for item in await self.media.list_items()
                if item.get("sourcePath") == source_path
            ),
            None,
        )
        if duplicate:
            raise AppError(409, f"该媒体已添加：{duplicate.get('name') or source_path}")

        cli_status = await self._cli_status()
        if not cli_status["available"] or not cli_status["loggedIn"]:
            raise AppError(409, "百度网盘授权状态已变化，请重新检查分享链接")
        files = [file for file in candidate["files"] if self._is_source_video(file.name)]
        if not files or any(not file.fsid for file in files):
            raise AppError(409, "分享媒体缺少可转存的文件标识，请重新检查")

        share_url = str(preview["shareUrl"])
        code = str(preview["extractCode"])
        base_destination = self.cli.normalize_destination(
            media_resource_path(
                self.config["saveRoot"],
                type_dir,
                category,
                folder_name,
            )
        )
        groups: dict[str, list[ShareMediaFile]] = {}
        for file in files:
            parent = "/".join(file.relative_parts[:-1])
            destination = "/".join(filter(None, [base_destination, parent]))
            groups.setdefault(destination, []).append(file)

        self._log(
            "info",
            f"开始转存百度网盘媒体：{folder_name}",
            fileCount=len(files),
            directoryCount=len(groups),
            destination=base_destination,
        )

        submitted_tasks: list[dict[str, str]] = []
        session_id = self.cli.new_session_id()
        now = self._iso_now()
        async with self._operation_lock:
            try:
                for group_index, (destination, group) in enumerate(
                    sorted(groups.items(), key=lambda item: item[0].casefold())
                ):
                    self._log(
                        "info",
                        f"正在提交转存目录 {group_index + 1}/{len(groups)}",
                        destination=destination,
                        fileCount=len(group),
                        files=self._file_names(group),
                    )
                    result = await self.cli.execute(
                        self.cli.select_command(
                            share_url,
                            [file.fsid for file in group],
                            destination,
                            code,
                            binary=self.settings.bdpan_binary,
                            session_id=session_id,
                        ),
                        timeout=self.settings.bdpan_timeout,
                        require_json=True,
                    )
                    task_id = self._task_id(result.payload)
                    if task_id:
                        submitted_tasks.append(
                            {"taskId": task_id, "destination": destination, "submittedAt": now}
                        )
                    self._log(
                        "success",
                        f"百度网盘转存任务已提交：{folder_name}",
                        destination=destination,
                        fileCount=len(group),
                        taskId=task_id or "未返回",
                    )
                    if group_index + 1 < len(groups):
                        await asyncio.sleep(2)
            except BdpanCliError as exc:
                raise AppError(502, str(exc)) from exc

            baidu_link = share_url + (
                ("&" if "?" in share_url else "?") + f"pwd={code}" if code else ""
            )
            item = await self.media.save_item(
                MediaItemInput(
                    source_path=source_path,
                    title=title,
                    year=year,
                    total_episodes=body.total_episodes,
                    season=body.season,
                    update_schedule=body.update_schedule,
                    category=category,
                    media_type=media_type,
                    status=body.status,
                    baidu_link=baidu_link,
                )
            )
            submitted_at = datetime.now(UTC)
            pending_at = submitted_at.isoformat()
            first_retry_at = (
                submitted_at + timedelta(seconds=int(self.config["settleSeconds"]))
            ).isoformat()
            self.states[item["id"]] = {
                "initialized": True,
                "shareKey": hashlib.sha256(
                    f"{share_url}\0{code}\0{candidate['prefix']}".encode()
                ).hexdigest(),
                "watchPrefix": candidate["prefix"],
                "seen": sorted(file.fingerprint for file in files),
                "lastCheckedAt": now,
                "lastTransferAt": now,
                "nextCheckAt": self._next_check_at(),
                "lastError": "",
                "failureCount": 0,
                "lastResult": f"已提交初始转存，共 {len(files)} 个媒体文件",
                "pendingSyncAt": pending_at,
                "firstRetrySyncAt": first_retry_at,
                "discoverParentBeforeSync": True,
                "pendingSyncAttempts": 0,
                "pendingFiles": ["/".join(file.relative_parts) for file in files],
                "submittedTasks": submitted_tasks,
            }
            await self._save_states()

        self._share_previews.pop(body.preview_id, None)
        self._wake.set()
        self._log(
            "success",
            f"百度网盘媒体已添加：{item['name']}，提交 {len(files)} 个文件",
            itemId=item["id"],
        )
        return {
            "item": item,
            "submittedCount": len(files),
            "taskCount": len(submitted_tasks),
            "pendingSyncAt": pending_at,
            "message": "转存已提交，正在立即执行首次扫描同步",
        }

    async def trigger_check(self, item_id: str = "") -> dict[str, Any]:
        if self._operation_lock.locked() or self._manual_check_pending:
            return {"started": False, "message": "已有百度网盘检查或同步任务正在运行"}
        if item_id:
            item = await self.media.get_item(item_id)
            if not item.get("baiduLink"):
                raise AppError(409, "该媒体未填写百度网盘分享链接")
        # Mark the request before yielding back to FastAPI.  Otherwise two HTTP
        # requests arriving in the same event-loop tick could both queue a run
        # before either task acquires the operation lock.
        self._manual_check_pending = True
        task = asyncio.create_task(self._run_requested(item_id), name="bdpan-check-now")
        self._track(task)
        self._log(
            "info",
            "百度网盘手动检查已加入队列",
            itemId=item_id or "all",
        )
        return {"started": True, "itemId": item_id or None}

    async def media_updated(self, item: dict[str, Any]) -> None:
        """Wake the scheduler when a watchable media record is saved."""
        if item.get("status") == "ongoing" and item.get("baiduLink"):
            state = self.states.setdefault(str(item["id"]), {})
            current_key = self._share_link_key(item)
            suspended_key = str(state.get("invalidShareKey") or "")
            if suspended_key and suspended_key != current_key:
                state.pop("invalidShareKey", None)
                state.pop("invalidShareAt", None)
                state.pop("linkInvalidNotificationKey", None)
                state["failureCount"] = 0
                state["lastError"] = ""
                state["lastResult"] = "分享链接已更新，自动追更已恢复"
                state["nextCheckAt"] = self._iso_now()
                self._log(
                    "success",
                    f"百度网盘分享链接已更新，恢复自动追更：{item['name']}",
                    itemId=item["id"],
                )
            elif not suspended_key:
                state["nextCheckAt"] = self._iso_now()
            await self._save_states()
        self._wake.set()

    async def forget_item(self, item_id: str) -> None:
        if self.states.pop(item_id, None) is not None:
            await self._save_states()
        self._wake.set()

    async def check_item(self, item_id: str) -> dict[str, Any]:
        async with self._operation_lock:
            self._running_item_id = item_id
            try:
                return await self._check_item_locked(item_id)
            finally:
                self._running_item_id = ""

    async def _check_item_locked(self, item_id: str) -> dict[str, Any]:
        item = await self.media.get_item(item_id)
        if item.get("status") != "ongoing":
            self._log("info", f"跳过百度网盘检查：{item['name']} 已完结", itemId=item_id)
            return {"itemId": item_id, "skipped": True, "reason": "媒体已完结"}
        if not item.get("baiduLink"):
            self._log("info", f"跳过百度网盘检查：{item['name']} 未配置分享链接", itemId=item_id)
            return {"itemId": item_id, "skipped": True, "reason": "未配置分享链接"}
        state = self.states.setdefault(item_id, {})
        if self._link_is_suspended(item, state):
            self._log(
                "info",
                f"跳过百度网盘检查：{item['name']} 的分享链接已失效，等待用户更新",
                itemId=item_id,
            )
            return {"itemId": item_id, "skipped": True, "reason": "分享链接失效，等待更新"}
        self._log(
            "info",
            f"开始检查百度网盘分享：{item['name']}",
            itemId=item_id,
            previousFileCount=len(state.get("seen") or []),
            failureCount=int(state.get("failureCount") or 0),
        )
        cli_status = await self._cli_status()
        if not cli_status["available"]:
            raise AppError(503, "bdpan CLI 未安装或配置路径不正确")
        if not cli_status["loggedIn"]:
            raise AppError(409, "bdpan 尚未完成百度网盘授权")

        try:
            share_url, extract_code = self.cli.parse_share_input(str(item["baiduLink"]))
            session_id = self.cli.new_session_id()
            if "watchPrefix" in state:
                raw_files = await self._list_share_media(
                    share_url,
                    extract_code,
                    session_id,
                    strip_wrapper=False,
                )
                files = self._files_for_prefix(raw_files, str(state.get("watchPrefix") or ""))
            else:
                files = await self._list_share_media(share_url, extract_code, session_id)
        except (BdpanCliError, AppError) as exc:
            await self._record_failure(item, exc)
            await self._suspend_invalid_link(item, exc)
            await self._notify_invalid_link_once(item, exc)
            raise

        state.pop("linkInvalidNotificationKey", None)

        discovered_count = len(files)
        files, duplicate_files = self._preferred_share_files(files, item)
        self._log(
            "info",
            f"百度网盘分享读取完成：{item['name']}",
            itemId=item_id,
            mediaFileCount=len(files),
            discoveredFileCount=discovered_count,
            skippedDuplicateCount=len(duplicate_files),
        )

        prefix = str(state.get("watchPrefix") or "") if "watchPrefix" in state else None
        share_key_source = f"{share_url}\0{extract_code}"
        if prefix is not None:
            share_key_source += f"\0{prefix}"
        share_key = hashlib.sha256(share_key_source.encode()).hexdigest()
        fingerprints = {file.fingerprint for file in files}
        previous = set(state.get("seen") or [])
        now = self._iso_now()
        share_changed = state.get("shareKey") != share_key
        was_initialized = bool(state.get("initialized"))
        reconcile = bool(state.pop("reconcileOnNextCheck", False))
        if share_changed or not was_initialized:
            state.update(
                {
                    "initialized": True,
                    "shareKey": share_key,
                    "seen": sorted(fingerprints),
                    "lastCheckedAt": now,
                    "nextCheckAt": self._next_check_at(),
                    "lastError": "",
                    "failureCount": 0,
                    "lastResult": (
                        f"Telegram 新链接已读取，共 {len(files)} 个媒体文件，正在核对本地缺集"
                        if reconcile and was_initialized
                        else f"已建立基线，共 {len(files)} 个媒体文件"
                    ),
                }
            )
            await self._save_states()
            if reconcile and was_initialized:
                previous = set(fingerprints)
            else:
                self._log(
                    "success",
                    f"百度网盘追更基线已建立：{item['name']}",
                    itemId=item_id,
                    fileCount=len(files),
                    nextCheckAt=state["nextCheckAt"],
                )
                return {
                    "itemId": item_id,
                    "baseline": True,
                    "fileCount": len(files),
                    "newCount": 0,
                }

        additions = [file for file in files if file.fingerprint not in previous]
        missing_files, quality_upgrades, inventory_checked = await self._saved_episode_repairs(
            item, files, state
        )
        transfer_candidates = {
            file.fingerprint: file for file in [*additions, *missing_files, *quality_upgrades]
        }
        ordered_candidates = sorted(
            transfer_candidates.values(),
            key=lambda file: self._transfer_sort_key(file, item),
        )
        if not ordered_candidates:
            state.update(
                {
                    "seen": sorted(previous | fingerprints),
                    "lastCheckedAt": now,
                    "nextCheckAt": self._next_check_at(),
                    "lastError": "",
                    "failureCount": 0,
                    "lastResult": "未发现新剧集，本地集数完整",
                }
            )
            await self._save_states()
            self._log(
                "info",
                f"百度网盘检查完成：{item['name']}，没有新剧集",
                itemId=item_id,
                fileCount=len(files),
                knownFileCount=len(previous | fingerprints),
                localInventoryChecked=inventory_checked,
                nextCheckAt=state["nextCheckAt"],
            )
            return {"itemId": item_id, "baseline": False, "fileCount": len(files), "newCount": 0}

        selected = ordered_candidates[: int(self.config["maxNewItems"])]
        missing_fingerprints = {file.fingerprint for file in missing_files}
        upgrade_fingerprints = {file.fingerprint for file in quality_upgrades}
        notification_fingerprints = (
            missing_fingerprints if inventory_checked else {file.fingerprint for file in additions}
        )
        self._log(
            "success",
            f"百度网盘检查到需转存内容：{item['name']}，共 {len(ordered_candidates)} 个文件",
            itemId=item_id,
            shareAdditionCount=len(additions),
            missingEpisodeCount=len(missing_files),
            qualityUpgradeCount=len(quality_upgrades),
            selectedCount=len(selected),
            deferredCount=max(0, len(ordered_candidates) - len(selected)),
            files=self._file_names(selected),
        )
        if any(not file.fsid for file in selected):
            error = BdpanCliError("分享列表缺少可用于选择性转存的文件标识")
            await self._record_failure(item, error)
            raise error

        transferred: list[ShareMediaFile] = []
        submitted_tasks: list[dict[str, str]] = []
        try:
            groups = self._group_transfers(item, selected)
            for group_index, (destination, group) in enumerate(groups):
                self._log(
                    "info",
                    f"正在提交追更转存 {group_index + 1}/{len(groups)}：{item['name']}",
                    itemId=item_id,
                    destination=destination,
                    fileCount=len(group),
                    files=self._file_names(group),
                )
                argv = self.cli.select_command(
                    share_url,
                    [file.fsid for file in group],
                    destination,
                    extract_code,
                    binary=self.settings.bdpan_binary,
                    session_id=session_id,
                )
                result = await self.cli.execute(
                    argv, timeout=self.settings.bdpan_timeout, require_json=True
                )
                task_id = self._task_id(result.payload)
                if task_id:
                    submitted_tasks.append(
                        {"taskId": task_id, "destination": destination, "submittedAt": now}
                    )
                transferred.extend(group)
                self._log(
                    "success",
                    f"追更转存任务已接受：{item['name']}",
                    itemId=item_id,
                    taskId=task_id or "未返回",
                    destination=destination,
                    fileCount=len(group),
                )
                if group_index + 1 < len(groups):
                    await asyncio.sleep(2)
        except (BdpanCliError, AppError) as exc:
            if transferred:
                previous.update(file.fingerprint for file in transferred)
                notification_count = sum(
                    file.fingerprint in notification_fingerprints for file in transferred
                )
                state.update(
                    {
                        "seen": sorted(previous),
                        "lastTransferAt": now,
                        "pendingSyncAt": (
                            datetime.now(UTC) + timedelta(seconds=int(self.config["settleSeconds"]))
                        ).isoformat(),
                        "pendingSyncAttempts": 0,
                        "pendingFiles": self._merge_pending_files(state, transferred),
                        "pendingNotificationNewCount": int(
                            state.get("pendingNotificationNewCount") or 0
                        )
                        + notification_count,
                        "pendingNotificationEpisodes": (
                            self._merge_pending_notification_episodes(
                                state,
                                transferred,
                                notification_fingerprints,
                                default_season=int(item.get("season") or 1),
                            )
                        ),
                        "submittedTasks": self._merge_submitted_tasks(state, submitted_tasks),
                    }
                )
            await self._record_failure(item, exc)
            await self._notify_invalid_link_once(item, exc)
            if transferred:
                self._wake.set()
            raise

        previous.update(file.fingerprint for file in transferred)
        notification_count = sum(
            file.fingerprint in notification_fingerprints for file in transferred
        )
        transferred_upgrades = sum(file.fingerprint in upgrade_fingerprints for file in transferred)
        state.update(
            {
                "seen": sorted(previous),
                "lastCheckedAt": now,
                "lastTransferAt": now,
                "nextCheckAt": self._next_check_at(),
                "lastError": "",
                "failureCount": 0,
                "lastResult": (
                    f"已提交 {len(transferred)} 个媒体文件"
                    f"（补齐 {notification_count} 集、质量升级 {transferred_upgrades} 集）"
                ),
                "pendingSyncAt": (
                    datetime.now(UTC) + timedelta(seconds=int(self.config["settleSeconds"]))
                ).isoformat(),
                "pendingSyncAttempts": 0,
                "pendingFiles": self._merge_pending_files(state, transferred),
                "pendingNotificationNewCount": int(state.get("pendingNotificationNewCount") or 0)
                + notification_count,
                "pendingNotificationEpisodes": self._merge_pending_notification_episodes(
                    state,
                    transferred,
                    notification_fingerprints,
                    default_season=int(item.get("season") or 1),
                ),
                "submittedTasks": self._merge_submitted_tasks(state, submitted_tasks),
            }
        )
        await self._save_states()
        self._log(
            "success",
            f"百度网盘发现更新：{item['name']}，已提交 {len(transferred)} 个文件",
            itemId=item_id,
            newCount=len(transferred),
            recoveredEpisodeCount=notification_count,
            qualityUpgradeCount=transferred_upgrades,
            nextCheckAt=state["nextCheckAt"],
            pendingSyncAt=state["pendingSyncAt"],
        )
        self._wake.set()
        return {
            "itemId": item_id,
            "baseline": False,
            "fileCount": len(files),
            "newCount": len(additions),
            "recoveredCount": notification_count,
            "qualityUpgradeCount": transferred_upgrades,
            "submittedCount": len(transferred),
        }

    async def _list_share_media(
        self,
        share_url: str,
        extract_code: str,
        session_id: str,
        *,
        strip_wrapper: bool = True,
    ) -> list[ShareMediaFile]:
        started_at = perf_counter()
        queue: list[tuple[str, tuple[str, ...], int]] = [("", (), 0)]
        visited: set[str] = set()
        files: list[ShareMediaFile] = []
        skipped_strm_count = 0
        request_count = 0
        while queue:
            source_dir, parent_parts, depth = queue.pop(0)
            if source_dir in visited or depth > 8:
                continue
            visited.add(source_dir)
            page = 1
            while True:
                request_count += 1
                if request_count > 80 or len(files) > 5_000:
                    raise BdpanCliError("分享目录内容过多，已停止自动检查")
                argv = self.cli.list_command(
                    share_url,
                    extract_code=extract_code,
                    source_dir=source_dir,
                    page=page,
                    binary=self.settings.bdpan_binary,
                    session_id=session_id,
                )
                result = await self.cli.execute(argv, timeout=90, require_json=True)
                items, has_more = self._share_page(result.payload)
                for raw in items:
                    name = str(
                        raw.get("server_filename")
                        or raw.get("name")
                        or posixpath.basename(str(raw.get("path") or ""))
                    ).strip()
                    if not name or name in {".", ".."} or "/" in name or "\\" in name:
                        continue
                    parts = (*parent_parts, name)
                    is_dir = raw.get("isdir") in {True, "1"} or raw.get("is_dir") is True
                    if is_dir:
                        child_path = str(raw.get("path") or "").strip()
                        if child_path:
                            queue.append((child_path, parts, depth + 1))
                        continue
                    suffix = PurePosixPath(name).suffix.casefold()
                    if suffix == ".strm":
                        skipped_strm_count += 1
                        continue
                    if suffix not in SOURCE_VIDEO_EXTENSIONS:
                        continue
                    fsid = str(raw.get("fsid") or raw.get("fs_id") or "")
                    files.append(
                        ShareMediaFile(
                            fsid=fsid,
                            name=name,
                            relative_parts=parts,
                            size=int(raw.get("size") or 0),
                            modified=str(raw.get("server_mtime") or raw.get("mtime") or ""),
                        )
                    )
                if not has_more or not items:
                    break
                page += 1
                await asyncio.sleep(0.35)
            if queue:
                await asyncio.sleep(0.35)
        result = self._strip_single_wrapper(files) if strip_wrapper else files
        self._log(
            "info",
            "百度网盘分享目录遍历完成",
            mediaFileCount=len(result),
            visitedDirectoryCount=len(visited),
            requestCount=request_count,
            skippedStrmReferenceCount=skipped_strm_count,
            durationMs=round((perf_counter() - started_at) * 1_000, 2),
        )
        if skipped_strm_count:
            self._log(
                "warning",
                f"已忽略 {skipped_strm_count} 个 STRM 引用文件，网盘源目录只转存原始视频",
                skippedStrmReferenceCount=skipped_strm_count,
            )
        return result

    def _group_transfers(
        self, item: dict[str, Any], files: list[ShareMediaFile]
    ) -> list[tuple[str, list[ShareMediaFile]]]:
        base = self._media_destination(item)
        grouped: dict[str, list[ShareMediaFile]] = {}
        for file in files:
            parent = "/".join(file.relative_parts[:-1])
            destination = "/".join(filter(None, [base, parent]))
            grouped.setdefault(destination, []).append(file)
        return sorted(grouped.items(), key=lambda entry: entry[0].casefold())

    def _media_destination(self, item: dict[str, Any]) -> str:
        relative = relative_virtual_path(self.path_config.list_root, item["sourcePath"])
        if relative is None:
            relative = "/".join(
                filter(
                    None,
                    [item.get("mediaType"), item.get("category"), item.get("name")],
                )
            )
        destination = "/".join(filter(None, [self.config["saveRoot"], relative]))
        return self.cli.normalize_destination(destination)

    async def _sync_item(self, item_id: str) -> None:
        async with self._operation_lock:
            self._running_item_id = item_id
            state = self.states.setdefault(item_id, {})
            try:
                item = await self.media.get_item(item_id)
                attempt = int(state.get("pendingSyncAttempts") or 0) + 1
                self._log(
                    "info",
                    f"开始处理百度网盘落盘同步：{item['name']}",
                    itemId=item_id,
                    attempt=attempt,
                    sourcePath=item["sourcePath"],
                )
                scan_path = str(item["sourcePath"])
                if state.get("discoverParentBeforeSync"):
                    scan_path = PurePosixPath(scan_path).parent.as_posix()
                await self.openlist.request(
                    "POST",
                    "/api/admin/scan/start",
                    {"path": scan_path, "limit": self.settings.scan_limit},
                )
                self._log(
                    "info",
                    f"OpenList STRM 扫描已启动：{item['name']}",
                    itemId=item_id,
                    scanPath=scan_path,
                    scanLimit=self.settings.scan_limit,
                    discoveringNewDirectory=bool(state.get("discoverParentBeforeSync")),
                )
                started = asyncio.get_running_loop().time()
                object_count = 0
                while True:
                    await asyncio.sleep(2)
                    progress = await self.openlist.request("GET", "/api/admin/scan/progress")
                    object_count = int((progress or {}).get("obj_count") or 0)
                    if bool((progress or {}).get("is_done")):
                        break
                    if asyncio.get_running_loop().time() - started > 600:
                        raise AppError(504, "OpenList 自动扫描超过 10 分钟")
                self._log(
                    "success",
                    f"OpenList STRM 扫描完成：{item['name']}",
                    itemId=item_id,
                    objectCount=object_count,
                    durationSeconds=round(asyncio.get_running_loop().time() - started, 1),
                )
                removed_source_manifests = await self._cleanup_shadowed_source_manifests(item)
                if removed_source_manifests:
                    # Existing target manifests may still point at the old,
                    # unclassified source location. Force a one-time rewrite
                    # after the real videos replace those source references.
                    await self.media.update_item(item_id, {"manifestVersion": 0})
                self._log(
                    "info",
                    f"开始整理并发布 STRM：{item['name']}",
                    itemId=item_id,
                    targetPath=item.get("targetDir") or "由媒体配置解析",
                )
                result = await self.media.publish(PublishRequest(id=item_id))
                current_item = await self.media.get_item(item_id)
                if self.emby302:
                    self.emby302.schedule_prewarm(
                        current_item,
                        result.get("warmupPaths") or [],
                    )
                copied = int(result.get("copied") or 0)
                new_files = len(result.get("newFiles") or [])
                episode_count = int(result.get("episodeCount") or 0)
                total_files = int(result.get("totalFiles") or 0)
                if copied and self.settings.emby_url and self.settings.emby_api_key:
                    await self.emby.refresh_library()
                    self._log(
                        "success",
                        f"Emby 媒体库刷新已触发：{item['name']}",
                        itemId=item_id,
                    )
                if self.media_probe:
                    self.media_probe.schedule(result.get("warmupPaths") or [])
                # A successful directory scan alone does not prove that every file in
                # an asynchronous bdpan task has landed. Compare the submitted episode
                # identities and quality with the underlying source videos before
                # marking the operation complete. The virtual Strm view is not proof:
                # a stale source .strm can expose the same episode name.
                state["pendingSyncAt"] = None
                expected_files = [str(value) for value in state.get("pendingFiles") or []]
                verification_files = [str(value) for value in current_item.get("syncedFiles") or []]
                verification_path = str(current_item.get("sourcePath") or "")
                if self.storage:
                    underlying_path = await self.storage.resolve_underlying_source_path(
                        verification_path
                    )
                    collect_files = getattr(self.media, "collect_files", None)
                    if underlying_path and callable(collect_files):
                        verification_path = underlying_path
                        verification_files = [
                            str(value)
                            for value in await collect_files(underlying_path)
                            if self._is_source_video(str(value))
                        ]
                missing_after_sync = self._pending_missing_files(
                    expected_files,
                    verification_files,
                    default_season=int(current_item.get("season") or 1),
                    media_type=str(current_item.get("mediaType") or "tv"),
                )
                if missing_after_sync:
                    state["pendingSyncAttempts"] = attempt
                    if attempt >= 6:
                        state["pendingSyncAt"] = None
                        state.pop("firstRetrySyncAt", None)
                        state["lastResult"] = (
                            f"转存落盘不完整，仍缺 {len(missing_after_sync)} 集，自动同步已停止重试"
                        )
                    else:
                        state["pendingSyncAt"] = self._next_sync_retry_at(state, attempt)
                        state["lastResult"] = (
                            f"同步后仍缺 {len(missing_after_sync)} 集，已安排落盘后重试"
                        )
                    state["lastError"] = "等待补齐：" + "、".join(missing_after_sync[:8])
                    state.pop("pendingNotificationNewCount", None)
                    state.pop("pendingNotificationEpisodes", None)
                    await self._save_states()
                    self._log(
                        "warning",
                        f"百度网盘转存落盘不完整：{item['name']}，仍缺 {len(missing_after_sync)} 集",
                        itemId=item_id,
                        missingFiles=missing_after_sync[:20],
                        verificationPath=verification_path,
                        nextRetryAt=state.get("pendingSyncAt") or "已停止重试",
                    )
                    self._wake.set()
                    return
                state.pop("pendingFiles", None)
                state.pop("firstRetrySyncAt", None)
                state.pop("discoverParentBeforeSync", None)
                state["pendingSyncAttempts"] = 0
                current_count = episode_count or total_files
                notification_count = int(state.pop("pendingNotificationNewCount", 0) or 0)
                notification_episodes = list(
                    dict.fromkeys(
                        [
                            *(str(value) for value in state.pop("pendingNotificationEpisodes", [])),
                            *(str(value) for value in result.get("newEpisodes") or []),
                        ]
                    )
                )
                if notification_episodes:
                    notification_count = len(notification_episodes)
                state["lastResult"] = (
                    f"转存落盘并同步完成，当前 {current_count} 集"
                    if episode_count
                    else f"转存落盘并同步完成，共 {total_files} 个文件"
                )
                state["lastSyncedAt"] = self._iso_now()
                state["lastError"] = ""
                await self._save_states()
                self._log(
                    "success",
                    f"百度网盘更新已同步：{item['name']}，当前 {current_count} 个媒体文件",
                    copied=copied,
                    newFiles=new_files,
                    episodeCount=episode_count,
                )
                if self.notifications and notification_count > 0:
                    await self.notifications.notify_episode_update(
                        item,
                        notification_count,
                        current_count,
                        notification_episodes,
                    )
            except Exception as exc:  # noqa: BLE001 - scheduler must retain failure state
                if state.get("firstRetrySyncAt") and self._is_source_not_ready_error(exc):
                    retry_at = self._next_sync_retry_at(state, 0)
                    state.update(
                        {
                            "pendingSyncAt": retry_at,
                            "pendingSyncAttempts": 0,
                            "lastResult": "转存目录尚未落盘，已安排自动重试同步",
                            "lastError": "",
                        }
                    )
                    await self._save_states()
                    self._log(
                        "info",
                        f"百度网盘转存目录尚未落盘，等待后重试同步：{item['name']}",
                        itemId=item_id,
                        nextRetryAt=retry_at,
                    )
                    self._wake.set()
                    return
                attempts = int(state.get("pendingSyncAttempts") or 0) + 1
                state["pendingSyncAttempts"] = attempts
                if attempts >= 6:
                    state["pendingSyncAt"] = None
                    state.pop("firstRetrySyncAt", None)
                    state["lastResult"] = "自动同步多次失败，等待下次发现更新后再试"
                else:
                    state["pendingSyncAt"] = self._next_sync_retry_at(state, attempts)
                state["lastError"] = str(exc)[:500]
                await self._save_states()
                self._log(
                    "error",
                    f"百度网盘自动同步失败：{str(exc)[:300]}",
                    itemId=item_id,
                    attempt=attempts,
                    nextRetryAt=state.get("pendingSyncAt") or "已停止重试",
                )
            finally:
                self._running_item_id = ""

    async def _record_failure(self, item: dict[str, Any], error: Exception) -> None:
        state = self.states.setdefault(item["id"], {})
        failures = int(state.get("failureCount") or 0) + 1
        delay_minutes = max(5, int(self.config["checkIntervalMinutes"]))
        if isinstance(error, BdpanCliError) and error.code == "13071":
            delay_minutes = max(delay_minutes, 5)
        else:
            delay_minutes = min(360, delay_minutes * (2 ** min(failures, 5)))
        state.update(
            {
                "lastCheckedAt": self._iso_now(),
                "nextCheckAt": (datetime.now(UTC) + timedelta(minutes=delay_minutes)).isoformat(),
                "lastError": str(error)[:500],
                "failureCount": failures,
                "lastResult": "检查失败，已降低检查频率",
            }
        )
        await self._save_states()
        self._log(
            "error",
            f"百度网盘检查失败：{item['name']} · {str(error)[:300]}",
            itemId=item["id"],
            failureCount=failures,
            nextCheckAt=state["nextCheckAt"],
        )

    async def _notify_invalid_link_once(self, item: dict[str, Any], error: Exception) -> None:
        if not self.notifications or not self._is_invalid_share_error(error):
            return
        state = self.states.setdefault(str(item["id"]), {})
        notification_key = self._share_link_key(item)
        if state.get("linkInvalidNotificationKey") == notification_key:
            return
        if await self.notifications.notify_link_invalid(item, error):
            state["linkInvalidNotificationKey"] = notification_key
            await self._save_states()

    async def _suspend_invalid_link(self, item: dict[str, Any], error: Exception) -> bool:
        if not self._is_invalid_share_error(error):
            return False
        state = self.states.setdefault(str(item["id"]), {})
        invalid_key = self._share_link_key(item)
        newly_suspended = state.get("invalidShareKey") != invalid_key
        state.update(
            {
                "invalidShareKey": invalid_key,
                "invalidShareAt": self._iso_now(),
                "nextCheckAt": None,
                "lastResult": "分享链接已失效，自动追更已暂停；更新链接后自动恢复",
                "lastError": str(error)[:500],
            }
        )
        await self._save_states()
        if newly_suspended:
            self._log(
                "warning",
                f"百度网盘分享链接失效，已暂停自动追更：{item['name']}",
                itemId=item["id"],
            )
        return True

    @staticmethod
    def _share_link_key(item: dict[str, Any]) -> str:
        return hashlib.sha256(str(item.get("baiduLink") or "").strip().encode()).hexdigest()

    @classmethod
    def _link_is_suspended(cls, item: dict[str, Any], state: dict[str, Any]) -> bool:
        invalid_key = str(state.get("invalidShareKey") or "")
        return bool(invalid_key and invalid_key == cls._share_link_key(item))

    @staticmethod
    def _is_invalid_share_error(error: Exception) -> bool:
        if isinstance(error, BdpanCliError) and error.code in {"13001", "13004"}:
            return True
        message = str(error).casefold()
        return any(
            phrase in message
            for phrase in (
                "errno=13001",
                "分享链接已失效",
                "链接已失效",
                "分享已取消",
                "分享链接不存在",
                "share link status is abnormal",
            )
        )

    async def _run_requested(self, item_id: str) -> None:
        try:
            if item_id:
                self._log("info", "百度网盘单项手动检查开始", itemId=item_id)
                await self.check_item(item_id)
                self._log("success", "百度网盘单项手动检查完成", itemId=item_id)
                return
            items = [
                item
                for item in await self.media.list_items()
                if item.get("status") == "ongoing" and item.get("baiduLink")
            ]
            self._log("info", "百度网盘批量手动检查开始", itemCount=len(items))
            success_count = 0
            failure_count = 0
            for index, item in enumerate(items):
                try:
                    await self.check_item(item["id"])
                    success_count += 1
                except Exception as exc:  # noqa: BLE001 - continue the requested batch
                    failure_count += 1
                    self._log(
                        "warning",
                        f"百度网盘手动检查已跳过 {item['name']}：{str(exc)[:240]}",
                        itemId=item["id"],
                    )
                if index + 1 < len(items):
                    await asyncio.sleep(2)
            self._log(
                "success" if not failure_count else "warning",
                "百度网盘批量手动检查完成",
                itemCount=len(items),
                successCount=success_count,
                failureCount=failure_count,
            )
        except Exception as exc:  # noqa: BLE001 - background request is reported in runtime log
            self._log("error", f"百度网盘手动检查失败：{str(exc)[:300]}")
        finally:
            self._manual_check_pending = False

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self._scheduler_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                self._log("error", f"百度网盘调度器异常：{str(exc)[:300]}")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except TimeoutError:
                pass

    async def _scheduler_tick(self) -> None:
        if self._operation_lock.locked() or self._manual_check_pending:
            return
        now = datetime.now(UTC)
        for item_id, state in list(self.states.items()):
            pending_at = self._parse_time(state.get("pendingSyncAt"))
            if pending_at and pending_at <= now:
                self._log(
                    "info",
                    "百度网盘转存等待时间已到，开始扫描同步",
                    itemId=item_id,
                    scheduledAt=state.get("pendingSyncAt"),
                )
                await self._sync_item(item_id)
                return
        if not self.config["enabled"]:
            return
        items = [
            item
            for item in await self.media.list_items()
            if item.get("status") == "ongoing" and item.get("baiduLink")
        ]
        for item in items:
            state = self.states.setdefault(item["id"], {})
            if self._link_is_suspended(item, state):
                continue
            due = self._parse_time(state.get("nextCheckAt"))
            if due is None or due <= now:
                self._log(
                    "info",
                    f"百度网盘定时检查已启动：{item['name']}",
                    itemId=item["id"],
                    scheduledAt=state.get("nextCheckAt") or "首次检查",
                )
                try:
                    await self.check_item(item["id"])
                except Exception as exc:  # noqa: BLE001 - failure state is stored by check_item
                    self._log("warning", f"百度网盘定时检查已延后：{str(exc)[:300]}")
                return

    async def _cli_status(self, *, refresh: bool = False) -> dict[str, Any]:
        loop_time = asyncio.get_running_loop().time()
        selected = self.settings.bdpan_binary
        if (
            not refresh
            and self._status_cache
            and loop_time - self._status_cache[0] < 30
            and self._status_cache[1].get("configuredBinary") == selected
        ):
            return dict(self._status_cache[1])
        try:
            status = await self.cli.status(selected)
            status["error"] = ""
        except Exception as exc:  # noqa: BLE001 - status endpoint should remain available
            status = {
                "available": self.cli.executable(selected) is not None,
                "loggedIn": False,
                "username": "",
                "expiresAt": "",
                "tokenExpiresIn": "",
                "version": "",
                "binary": selected,
                "error": str(exc)[:300],
            }
        status["configuredBinary"] = selected
        self._status_cache = (loop_time, status)
        return dict(status)

    def _public_watch(self, item: dict[str, Any]) -> dict[str, Any]:
        state = self.states.get(item["id"], {})
        return {
            "itemId": item["id"],
            "name": item.get("name") or item.get("title"),
            "destination": self._media_destination(item),
            "initialized": bool(state.get("initialized")),
            "seenCount": len(state.get("seen") or []),
            "lastCheckedAt": state.get("lastCheckedAt"),
            "nextCheckAt": state.get("nextCheckAt"),
            "lastTransferAt": state.get("lastTransferAt"),
            "lastSyncedAt": state.get("lastSyncedAt"),
            "pendingSync": bool(state.get("pendingSyncAt")),
            "suspended": self._link_is_suspended(item, state),
            "submittedTaskCount": len(state.get("submittedTasks") or []),
            "lastResult": state.get("lastResult") or "等待首次检查",
            "lastError": state.get("lastError") or "",
        }

    async def _save_states(self) -> None:
        async with self._state_lock:
            await self.repository.save_bdpan_watch_states(self.states)

    def _default_config(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.bdpan_enabled,
            "trackingMode": "polling",
            "checkIntervalMinutes": self.settings.bdpan_check_interval_minutes,
            "saveRoot": self.cli.normalize_destination(self.settings.bdpan_save_root),
            "settleSeconds": self.settings.bdpan_settle_seconds,
            "maxNewItems": self.settings.bdpan_max_new_items,
        }

    def _normalize_config(self, value: dict[str, Any]) -> dict[str, Any]:
        save_root = self.cli.normalize_destination(
            str(value.get("saveRoot") or self.settings.bdpan_save_root)
        )
        if not save_root:
            raise AppError(400, "请设置百度网盘转存根目录")
        return {
            "enabled": bool(value.get("enabled", False)),
            "trackingMode": "hybrid" if value.get("trackingMode") == "hybrid" else "polling",
            "checkIntervalMinutes": max(5, min(1440, int(value.get("checkIntervalMinutes") or 10))),
            "saveRoot": save_root,
            "settleSeconds": max(30, min(1800, int(value.get("settleSeconds") or 90))),
            "maxNewItems": max(1, min(100, int(value.get("maxNewItems") or 20))),
        }

    async def telegram_update(
        self,
        item: dict[str, Any],
        *,
        link_changed: bool,
        source: str,
        episode: int | None,
    ) -> None:
        """Resume and prioritize one watch after a matched Telegram announcement."""
        state = self.states.setdefault(str(item["id"]), {})
        if link_changed:
            state.pop("invalidShareKey", None)
            state.pop("invalidShareAt", None)
            state.pop("linkInvalidNotificationKey", None)
        elif self._link_is_suspended(item, state):
            state["lastResult"] = f"Telegram 已匹配 {source}，但分享链接未变更，继续暂停追更"
            await self._save_states()
            self._log(
                "warning",
                f"Telegram 更新仍为已失效链接：{item['name']}",
                itemId=item["id"],
                source=source,
                episode=episode,
            )
            return
        state["failureCount"] = 0
        state["lastError"] = ""
        state["nextCheckAt"] = self._iso_now()
        if link_changed and state.get("initialized"):
            state["reconcileOnNextCheck"] = True
        suffix = f"，消息提示更新至 {episode} 集" if episode else ""
        if not self.config["enabled"]:
            state["lastResult"] = f"Telegram 已匹配 {source}{suffix}，但百度网盘自动追更未启用"
            await self._save_states()
            self._log(
                "warning",
                f"Telegram 更新未执行检查：{item['name']}，百度网盘自动追更未启用",
                itemId=item["id"],
                source=source,
                episode=episode,
                linkChanged=link_changed,
            )
            return

        can_start_now = not self._operation_lock.locked() and not self._manual_check_pending
        state["lastResult"] = (
            f"Telegram 已匹配更新：{source}{suffix}，已加入即时检查"
            if can_start_now
            else f"Telegram 已匹配更新：{source}{suffix}，等待当前网盘任务完成"
        )
        if can_start_now:
            self._manual_check_pending = True
        try:
            await self._save_states()
        except Exception:
            if can_start_now:
                self._manual_check_pending = False
            raise
        self._wake.set()
        if can_start_now:
            task = asyncio.create_task(
                self._run_telegram_check(str(item["id"]), source),
                name="bdpan-telegram-check",
            )
            self._track(task)
        self._log(
            "success",
            f"Telegram 更新已匹配：{item['name']}",
            itemId=item["id"],
            source=source,
            episode=episode,
            linkChanged=link_changed,
            immediateCheck=can_start_now,
        )

    async def _run_telegram_check(self, item_id: str, source: str) -> None:
        try:
            self._log(
                "info",
                "Telegram 已触发百度网盘即时检查",
                itemId=item_id,
                source=source,
            )
            result = await self.check_item(item_id)
            self._log(
                "success",
                "Telegram 触发的百度网盘检查已完成",
                itemId=item_id,
                source=source,
                newCount=int(result.get("newCount") or 0),
                baseline=bool(result.get("baseline")),
                skipped=bool(result.get("skipped")),
            )
        except Exception as exc:  # noqa: BLE001 - background failure is reported in runtime log
            self._log(
                "error",
                f"Telegram 触发的百度网盘检查失败：{str(exc)[:300]}",
                itemId=item_id,
                source=source,
            )
        finally:
            self._manual_check_pending = False
            self._wake.set()

    def _next_check_at(self) -> str:
        interval = int(self.config["checkIntervalMinutes"]) * 60
        jittered = max(300, interval + random.randint(-max(1, interval // 10), interval // 10))
        return (datetime.now(UTC) + timedelta(seconds=jittered)).isoformat()

    def _prune_share_previews(self, now: float) -> None:
        self._share_previews = {
            key: value
            for key, value in self._share_previews.items()
            if now - float(value.get("createdAt") or 0) < 900
        }
        while len(self._share_previews) >= 20:
            oldest = min(
                self._share_previews,
                key=lambda key: float(self._share_previews[key].get("createdAt") or 0),
            )
            self._share_previews.pop(oldest, None)

    def _get_share_preview(self, preview_id: str) -> dict[str, Any]:
        now = asyncio.get_running_loop().time()
        self._prune_share_previews(now)
        preview = self._share_previews.get(str(preview_id))
        if not preview:
            raise AppError(404, "分享检查结果已过期，请重新检查分享链接")
        return preview

    @classmethod
    def _share_candidates(cls, files: list[ShareMediaFile]) -> list[dict[str, Any]]:
        grouped: dict[str, list[ShareMediaFile]] = {}
        for file in files:
            if not cls._is_source_video(file.name):
                continue
            prefix = ""
            if len(file.relative_parts) > 1 and not SEASON_DIRECTORY.match(file.relative_parts[0]):
                prefix = file.relative_parts[0]
            grouped.setdefault(prefix, []).append(file)

        candidates: list[dict[str, Any]] = []
        for prefix, grouped_files in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            normalized = cls._files_for_prefix(grouped_files, prefix)
            all_fingerprints = sorted(file.fingerprint for file in normalized)
            normalized, duplicates = select_preferred_episodes(
                normalized,
                path=lambda file: "/".join(file.relative_parts),
                size=lambda file: file.size,
            )
            name = prefix or cls._infer_media_name(normalized)
            match = re.match(r"^(.*?)\s*[（(](\d{4})[）)]\s*$", name)
            title = match.group(1).strip() if match else name
            year = match.group(2) if match else ""
            identity = "\0".join([prefix, *sorted(file.fingerprint for file in normalized)])
            candidates.append(
                {
                    "id": "c" + hashlib.sha256(identity.encode()).hexdigest()[:16],
                    "prefix": prefix,
                    "name": name,
                    "title": title,
                    "year": year,
                    "fileCount": len(normalized),
                    "duplicateCount": len(duplicates),
                    "totalBytes": sum(max(0, file.size) for file in normalized),
                    "sampleFiles": ["/".join(file.relative_parts) for file in normalized[:5]],
                    "files": normalized,
                    "allFingerprints": all_fingerprints,
                }
            )
        return candidates

    @staticmethod
    def _preferred_share_files(
        files: list[ShareMediaFile], item: dict[str, Any]
    ) -> tuple[list[ShareMediaFile], list[ShareMediaFile]]:
        files = [file for file in files if BdpanAutomationService._is_source_video(file.name)]
        if item.get("mediaType") != "tv":
            return files, []
        return select_preferred_episodes(
            files,
            path=lambda file: "/".join(file.relative_parts),
            size=lambda file: file.size,
            default_season=int(item.get("season") or 1),
        )

    async def _saved_episode_repairs(
        self,
        item: dict[str, Any],
        files: list[ShareMediaFile],
        state: dict[str, Any],
    ) -> tuple[list[ShareMediaFile], list[ShareMediaFile], bool]:
        """Find missing episodes and better variants already hidden by the watch baseline."""
        if item.get("mediaType") != "tv" or state.get("pendingSyncAt"):
            return [], [], False
        collect_files = getattr(self.media, "collect_files", None)
        if not callable(collect_files):
            return [], [], False
        inventory_path = str(item["sourcePath"])
        try:
            saved_files = await collect_files(inventory_path)
            if self.storage:
                underlying_path = await self.storage.resolve_underlying_source_path(inventory_path)
                if underlying_path:
                    underlying_files = await collect_files(underlying_path)
                    source_strm_count = sum(
                        str(value).casefold().endswith(".strm") for value in underlying_files
                    )
                    saved_files = [
                        str(value)
                        for value in underlying_files
                        if self._is_source_video(str(value))
                    ]
                    inventory_path = underlying_path
                    if source_strm_count:
                        self._log(
                            "warning",
                            f"检测到网盘源目录混入 {source_strm_count} 个 STRM 引用文件",
                            itemId=item.get("id") or "",
                            sourcePath=underlying_path,
                            sourceStrmCount=source_strm_count,
                        )
        except (AppError, TypeError) as exc:
            self._log(
                "warning",
                f"读取已保存剧集清单失败，暂只按分享增量检查：{str(exc)[:240]}",
                itemId=item.get("id") or "",
            )
            return [], [], False
        missing, upgrades = self._episode_repairs(
            files,
            saved_files,
            default_season=int(item.get("season") or 1),
        )
        if missing or upgrades:
            self._log(
                "warning" if missing else "info",
                f"本地剧集完整性检查：{item['name']}，缺失 {len(missing)} 集、可升级 {len(upgrades)} 集",
                itemId=item.get("id") or "",
                savedFileCount=len(saved_files),
                inventoryPath=inventory_path,
                missingFiles=self._file_names(missing),
                upgradeFiles=self._file_names(upgrades),
            )
        return missing, upgrades, True

    async def _cleanup_shadowed_source_manifests(self, item: dict[str, Any]) -> int:
        """Remove stale source STRMs only after an equal/better real video has landed."""
        if not self.storage:
            return 0
        underlying_path = await self.storage.resolve_underlying_source_path(item["sourcePath"])
        if not underlying_path:
            return 0
        collect_files = getattr(self.media, "collect_files", None)
        if not callable(collect_files):
            return 0
        files = [str(value) for value in await collect_files(underlying_path)]
        default_season = int(item.get("season") or 1)
        videos: dict[tuple[int, int], list[str]] = {}
        for path in files:
            if not self._is_source_video(path):
                continue
            season, episode = source_season_episode(path)
            if episode is not None:
                videos.setdefault((season or default_season, episode), []).append(path)

        redundant: list[str] = []
        for path in files:
            if not path.casefold().endswith(".strm"):
                continue
            season, episode = source_season_episode(path)
            if episode is None:
                continue
            real_videos = videos.get((season or default_season, episode), [])
            if (
                real_videos
                and max(media_quality_rank(value)[:-1] for value in real_videos)
                >= (media_quality_rank(path)[:-1])
            ):
                redundant.append(path)
        if not redundant:
            return 0

        groups: dict[str, list[str]] = {}
        for relative in redundant:
            directory, separator, name = relative.rpartition("/")
            groups.setdefault(directory if separator else "", []).append(name or relative)
        for directory, names in groups.items():
            await self.openlist.remove(join_virtual_path(underlying_path, directory), names)
        self._log(
            "success",
            f"已清理网盘源目录中 {len(redundant)} 个被原始视频替代的 STRM 引用文件",
            itemId=item.get("id") or "",
            sourcePath=underlying_path,
            removedFiles=redundant[:20],
        )
        return len(redundant)

    @staticmethod
    def _is_source_video(path: str) -> bool:
        return PurePosixPath(str(path)).suffix.casefold() in SOURCE_VIDEO_EXTENSIONS

    @staticmethod
    def _episode_repairs(
        share_files: list[ShareMediaFile],
        saved_files: list[str],
        *,
        default_season: int = 1,
    ) -> tuple[list[ShareMediaFile], list[ShareMediaFile]]:
        saved_by_episode: dict[tuple[int, int], list[str]] = {}
        for path in saved_files:
            season, episode = source_season_episode(path)
            if episode is None:
                continue
            saved_by_episode.setdefault((season or default_season, episode), []).append(path)

        missing: list[ShareMediaFile] = []
        upgrades: list[ShareMediaFile] = []
        for file in share_files:
            path = "/".join(file.relative_parts)
            season, episode = source_season_episode(path)
            if episode is None:
                continue
            existing = saved_by_episode.get((season or default_season, episode), [])
            if not existing:
                missing.append(file)
                continue
            # The saved object is a small STRM manifest, so its byte size is not
            # comparable with the shared video. Compare only filename quality
            # characteristics; current-share selection still uses video size as
            # the final tie breaker between otherwise identical variants.
            incoming_rank = media_quality_rank(path)[:-1]
            saved_rank = max(media_quality_rank(value)[:-1] for value in existing)
            if incoming_rank > saved_rank:
                upgrades.append(file)
        return missing, upgrades

    @staticmethod
    def _transfer_sort_key(
        file: ShareMediaFile, item: dict[str, Any]
    ) -> tuple[int, int, str, tuple[str, ...]]:
        season, episode = source_season_episode("/".join(file.relative_parts))
        return (
            season or int(item.get("season") or 1),
            episode if episode is not None else 1_000_000,
            file.modified,
            tuple(part.casefold() for part in file.relative_parts),
        )

    @staticmethod
    def _files_for_prefix(files: list[ShareMediaFile], prefix: str) -> list[ShareMediaFile]:
        selected: list[ShareMediaFile] = []
        for file in files:
            parts = file.relative_parts
            if prefix:
                if len(parts) < 2 or parts[0] != prefix:
                    continue
                parts = parts[1:]
            elif len(parts) > 1 and not SEASON_DIRECTORY.match(parts[0]):
                continue
            selected.append(
                ShareMediaFile(
                    fsid=file.fsid,
                    name=file.name,
                    relative_parts=parts,
                    size=file.size,
                    modified=file.modified,
                )
            )
        return selected

    @staticmethod
    def _infer_media_name(files: list[ShareMediaFile]) -> str:
        if not files:
            return "新媒体"
        stem = PurePosixPath(files[0].name).stem
        stem = re.sub(
            r"(?i)[ ._-]*(?:S\d{1,2}[ ._-]*E\d{1,4}|EP?\d{1,4}|第\s*\d+\s*集).*$",
            "",
            stem,
        )
        stem = re.sub(r"[._]+", " ", stem).strip(" -_")
        return stem or "新媒体"

    @staticmethod
    def _share_page(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        body = payload
        if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
            body = body["data"]
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)], False
        if not isinstance(body, dict):
            return [], False
        raw_items = body.get("items") or body.get("list") or body.get("files") or []
        items = [item for item in raw_items if isinstance(item, dict)]
        return items, bool(body.get("has_more") or body.get("hasMore"))

    @staticmethod
    def _task_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        data = payload.get("data")
        candidates = [
            payload.get("task_id"),
            payload.get("taskId"),
            data.get("task_id") if isinstance(data, dict) else None,
            data.get("taskId") if isinstance(data, dict) else None,
        ]
        return next((str(value) for value in candidates if value not in {None, ""}), "")[:200]

    @staticmethod
    def _merge_submitted_tasks(
        state: dict[str, Any], submitted: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        previous = [
            dict(value) for value in state.get("submittedTasks") or [] if isinstance(value, dict)
        ]
        return [*previous, *submitted][-20:]

    @staticmethod
    def _merge_pending_files(state: dict[str, Any], submitted: list[ShareMediaFile]) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    *(str(value) for value in state.get("pendingFiles") or []),
                    *("/".join(file.relative_parts) for file in submitted),
                ]
            )
        )

    @staticmethod
    def _merge_pending_notification_episodes(
        state: dict[str, Any],
        submitted: list[ShareMediaFile],
        notification_fingerprints: set[str],
        *,
        default_season: int,
    ) -> list[str]:
        episodes = {
            str(value).upper()
            for value in state.get("pendingNotificationEpisodes") or []
            if re.fullmatch(r"(?i)S\d{1,3}E\d{1,4}", str(value).strip())
        }
        for file in submitted:
            if file.fingerprint not in notification_fingerprints:
                continue
            season, episode = source_season_episode("/".join(file.relative_parts))
            if episode is not None:
                episodes.add(f"S{season or max(1, default_season):02d}E{episode:02d}")
        return sorted(episodes)

    @staticmethod
    def _pending_missing_files(
        expected_files: list[str],
        saved_files: list[str],
        *,
        default_season: int = 1,
        media_type: str = "tv",
    ) -> list[str]:
        if not expected_files:
            return []
        if media_type != "tv":
            saved_stems = {PurePosixPath(value).stem.casefold() for value in saved_files}
            return [
                PurePosixPath(value).name
                for value in expected_files
                if PurePosixPath(value).stem.casefold() not in saved_stems
            ]

        saved_by_episode: dict[tuple[int, int], tuple[int, ...]] = {}
        for value in saved_files:
            season, episode = source_season_episode(value)
            if episode is None:
                continue
            identity = (season or default_season, episode)
            rank = media_quality_rank(value)[:-1]
            saved_by_episode[identity] = max(saved_by_episode.get(identity, rank), rank)

        missing: list[str] = []
        for value in expected_files:
            season, episode = source_season_episode(value)
            if episode is None:
                if not any(
                    PurePosixPath(saved).stem.casefold() == PurePosixPath(value).stem.casefold()
                    for saved in saved_files
                ):
                    missing.append(PurePosixPath(value).name)
                continue
            saved_rank = saved_by_episode.get((season or default_season, episode))
            if saved_rank is None or saved_rank < media_quality_rank(value)[:-1]:
                missing.append(PurePosixPath(value).name)
        return missing

    @staticmethod
    def _strip_single_wrapper(files: list[ShareMediaFile]) -> list[ShareMediaFile]:
        first_parts = {file.relative_parts[0] for file in files if len(file.relative_parts) > 1}
        if (
            not files
            or len(first_parts) != 1
            or any(len(file.relative_parts) < 2 for file in files)
        ):
            return files
        wrapper = next(iter(first_parts))
        # A share often wraps a show in one top-level folder.  Strip that
        # duplicate show name, but keep a lone Season/Sxx/第x季 directory so
        # media naming can still identify the correct season.
        if SEASON_DIRECTORY.match(wrapper):
            return files
        return [
            ShareMediaFile(
                fsid=file.fsid,
                name=file.name,
                relative_parts=file.relative_parts[1:],
                size=file.size,
                modified=file.modified,
            )
            for file in files
        ]

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

    def _next_sync_retry_at(self, state: dict[str, Any], attempts: int) -> str:
        """Keep the configured settle delay as fallback after the immediate first sync."""
        current = datetime.now(UTC)
        first_retry = self._parse_time(state.pop("firstRetrySyncAt", None))
        if first_retry and first_retry > current:
            return first_retry.isoformat()
        delay = min(60, 5 * (2 ** min(attempts, 3)))
        return (current + timedelta(minutes=delay)).isoformat()

    @staticmethod
    def _is_source_not_ready_error(error: Exception) -> bool:
        message = str(error).casefold()
        return any(
            phrase in message
            for phrase in (
                "object not found",
                "failed get dir",
                "directory not found",
                "path not found",
                "目录不存在",
                "对象不存在",
            )
        )

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(UTC).isoformat()

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _file_names(files: list[ShareMediaFile], limit: int = 12) -> list[str]:
        names = ["/".join(file.relative_parts) for file in files[:limit]]
        if len(files) > limit:
            names.append(f"…另有 {len(files) - limit} 个文件")
        return names

    def _log(self, level: str, message: str, **details: Any) -> None:
        self.runtime_logs.add(category="bdpan", level=level, message=message, **details)
