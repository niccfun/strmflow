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
from typing import Any

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
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
from strmflow.utils.paths import join_virtual_path, relative_virtual_path, validate_folder_name

VIDEO_EXTENSIONS = {
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
    ".strm",
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
    ) -> None:
        self.settings = settings
        self.cli = cli
        self.repository = repository
        self.media = media
        self.openlist = openlist
        self.emby = emby
        self.path_config = path_config
        self.runtime_logs = runtime_logs
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
        self._scheduler = asyncio.create_task(self._scheduler_loop(), name="bdpan-scheduler")
        self._log(
            "success",
            "百度网盘自动追更调度器已启动",
            enabled=self.config["enabled"],
            intervalMinutes=self.config["checkIntervalMinutes"],
        )

    async def close(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
        for task in self._tasks:
            task.cancel()
        pending = [task for task in [self._scheduler, *self._tasks] if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

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
        selected = str(self.config["binary"])
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
            status = await self._cli_status(binary=config["binary"], refresh=True)
            if not status["available"]:
                raise AppError(409, f"未找到 bdpan 二进制：{config['binary']}")
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
            intervalMinutes=config["checkIntervalMinutes"],
            maxNewItems=config["maxNewItems"],
        )
        return await self.status(refresh=True)

    async def start_login(self, accepted: bool) -> dict[str, Any]:
        if not accepted:
            raise AppError(400, "请先阅读并确认百度网盘安全提示")
        try:
            url = await self.cli.start_login(self.config["binary"])
        except BdpanCliError as exc:
            raise AppError(502, str(exc)) from exc
        self._log("info", "百度网盘授权链接已生成")
        return {"authorizationUrl": url, "expiresIn": 600}

    async def complete_login(self, code: str) -> dict[str, Any]:
        try:
            result = await self.cli.complete_login(code, self.config["binary"])
        except BdpanCliError as exc:
            raise AppError(502, str(exc)) from exc
        self._status_cache = None
        self._quota_cache = None
        self._wake.set()
        self._log("success", "百度网盘授权已完成")
        return result

    async def inspect_share(self, value: str, extract_code: str = "") -> dict[str, Any]:
        """Inspect a share once and keep its file identifiers only in short-lived memory."""
        cli_status = await self._cli_status()
        if not cli_status["available"]:
            raise AppError(503, "bdpan CLI 未安装或配置路径不正确")
        if not cli_status["loggedIn"]:
            raise AppError(409, "请先在系统设置中完成百度网盘授权")
        if self._operation_lock.locked():
            raise AppError(409, "已有百度网盘任务正在运行，请稍后重试")

        share_url, code = self.cli.parse_share_input(value, extract_code)
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
            raise AppError(409, "分享中未发现可保存的视频或 STRM 媒体文件")

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
        """Transfer an inspected share into the selected two-level media directory."""
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
        title = validate_folder_name(body.title)
        year = str(body.year or "").strip()
        if year and not re.fullmatch(r"\d{4}", year):
            raise AppError(400, "年份必须是 4 位数字")
        folder_name = validate_folder_name(f"{title} ({year})" if year else title)
        if not self.path_config.list_root:
            raise AppError(409, "请先设置只读源 STRM 根目录")
        source_path = join_virtual_path(
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
        files = list(candidate["files"])
        if not files or any(not file.fsid for file in files):
            raise AppError(409, "分享媒体缺少可转存的文件标识，请重新检查")

        share_url = str(preview["shareUrl"])
        code = str(preview["extractCode"])
        base_destination = "/".join(
            filter(None, [self.config["saveRoot"], type_dir, category, folder_name])
        )
        groups: dict[str, list[ShareMediaFile]] = {}
        for file in files:
            parent = "/".join(file.relative_parts[:-1])
            destination = "/".join(filter(None, [base_destination, parent]))
            groups.setdefault(destination, []).append(file)

        submitted_tasks: list[dict[str, str]] = []
        session_id = self.cli.new_session_id()
        now = self._iso_now()
        async with self._operation_lock:
            try:
                for group_index, (destination, group) in enumerate(
                    sorted(groups.items(), key=lambda item: item[0].casefold())
                ):
                    result = await self.cli.execute(
                        self.cli.select_command(
                            share_url,
                            [file.fsid for file in group],
                            destination,
                            code,
                            binary=self.config["binary"],
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
                    media_type=body.media_type,
                    status=body.status,
                    baidu_link=baidu_link,
                )
            )
            pending_at = (
                datetime.now(UTC) + timedelta(seconds=int(self.config["settleSeconds"]))
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
                "pendingSyncAttempts": 0,
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
            "message": "转存已提交，文件落盘后将自动扫描并同步到 Emby",
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
            self.states.setdefault(str(item["id"]), {})["nextCheckAt"] = self._iso_now()
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
            return {"itemId": item_id, "skipped": True, "reason": "媒体已完结"}
        if not item.get("baiduLink"):
            return {"itemId": item_id, "skipped": True, "reason": "未配置分享链接"}
        cli_status = await self._cli_status()
        if not cli_status["available"]:
            raise AppError(503, "bdpan CLI 未安装或配置路径不正确")
        if not cli_status["loggedIn"]:
            raise AppError(409, "bdpan 尚未完成百度网盘授权")

        state = self.states.setdefault(item_id, {})
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
            raise

        prefix = str(state.get("watchPrefix") or "") if "watchPrefix" in state else None
        share_key_source = f"{share_url}\0{extract_code}"
        if prefix is not None:
            share_key_source += f"\0{prefix}"
        share_key = hashlib.sha256(share_key_source.encode()).hexdigest()
        fingerprints = {file.fingerprint for file in files}
        previous = set(state.get("seen") or [])
        now = self._iso_now()
        if state.get("shareKey") != share_key or not state.get("initialized"):
            state.update(
                {
                    "initialized": True,
                    "shareKey": share_key,
                    "seen": sorted(fingerprints),
                    "lastCheckedAt": now,
                    "nextCheckAt": self._next_check_at(),
                    "lastError": "",
                    "failureCount": 0,
                    "lastResult": f"已建立基线，共 {len(files)} 个媒体文件",
                }
            )
            await self._save_states()
            self._log("success", f"百度网盘追更基线已建立：{item['name']}", fileCount=len(files))
            return {"itemId": item_id, "baseline": True, "fileCount": len(files), "newCount": 0}

        additions = [file for file in files if file.fingerprint not in previous]
        additions.sort(key=lambda file: (file.modified, file.relative_parts))
        if not additions:
            state.update(
                {
                    "seen": sorted(previous | fingerprints),
                    "lastCheckedAt": now,
                    "nextCheckAt": self._next_check_at(),
                    "lastError": "",
                    "failureCount": 0,
                    "lastResult": "未发现新剧集",
                }
            )
            await self._save_states()
            self._log("info", f"百度网盘检查完成：{item['name']}，没有新剧集")
            return {"itemId": item_id, "baseline": False, "fileCount": len(files), "newCount": 0}

        selected = additions[: int(self.config["maxNewItems"])]
        if any(not file.fsid for file in selected):
            error = BdpanCliError("分享列表缺少可用于选择性转存的文件标识")
            await self._record_failure(item, error)
            raise error

        transferred: list[ShareMediaFile] = []
        submitted_tasks: list[dict[str, str]] = []
        try:
            groups = self._group_transfers(item, selected)
            for group_index, (destination, group) in enumerate(groups):
                argv = self.cli.select_command(
                    share_url,
                    [file.fsid for file in group],
                    destination,
                    extract_code,
                    binary=self.config["binary"],
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
                if group_index + 1 < len(groups):
                    await asyncio.sleep(2)
        except (BdpanCliError, AppError) as exc:
            if transferred:
                previous.update(file.fingerprint for file in transferred)
                state.update(
                    {
                        "seen": sorted(previous),
                        "lastTransferAt": now,
                        "pendingSyncAt": (
                            datetime.now(UTC) + timedelta(seconds=int(self.config["settleSeconds"]))
                        ).isoformat(),
                        "pendingSyncAttempts": 0,
                        "submittedTasks": self._merge_submitted_tasks(state, submitted_tasks),
                    }
                )
            await self._record_failure(item, exc)
            if transferred:
                self._wake.set()
            raise

        previous.update(file.fingerprint for file in transferred)
        state.update(
            {
                "seen": sorted(previous),
                "lastCheckedAt": now,
                "lastTransferAt": now,
                "nextCheckAt": self._next_check_at(),
                "lastError": "",
                "failureCount": 0,
                "lastResult": f"已提交 {len(transferred)} 个新媒体文件",
                "pendingSyncAt": (
                    datetime.now(UTC) + timedelta(seconds=int(self.config["settleSeconds"]))
                ).isoformat(),
                "pendingSyncAttempts": 0,
                "submittedTasks": self._merge_submitted_tasks(state, submitted_tasks),
            }
        )
        await self._save_states()
        self._log(
            "success",
            f"百度网盘发现更新：{item['name']}，已提交 {len(transferred)} 个文件",
            newCount=len(transferred),
        )
        self._wake.set()
        return {
            "itemId": item_id,
            "baseline": False,
            "fileCount": len(files),
            "newCount": len(additions),
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
        queue: list[tuple[str, tuple[str, ...], int]] = [("", (), 0)]
        visited: set[str] = set()
        files: list[ShareMediaFile] = []
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
                    binary=self.config["binary"],
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
                    if PurePosixPath(name).suffix.casefold() not in VIDEO_EXTENSIONS:
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
        return self._strip_single_wrapper(files) if strip_wrapper else files

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
                await self.openlist.request(
                    "POST",
                    "/api/admin/scan/start",
                    {"path": item["sourcePath"], "limit": self.settings.scan_limit},
                )
                started = asyncio.get_running_loop().time()
                while True:
                    await asyncio.sleep(2)
                    progress = await self.openlist.request("GET", "/api/admin/scan/progress")
                    if bool((progress or {}).get("is_done")):
                        break
                    if asyncio.get_running_loop().time() - started > 600:
                        raise AppError(504, "OpenList 自动扫描超过 10 分钟")
                result = await self.media.publish(PublishRequest(id=item_id))
                copied = int(result.get("copied") or 0)
                new_files = len(result.get("newFiles") or [])
                if copied and self.settings.emby_url and self.settings.emby_api_key:
                    await self.emby.refresh_library()
                attempts = int(state.get("pendingSyncAttempts") or 0)
                waiting_for_source = new_files == 0 and copied == 0 and attempts < 3
                if waiting_for_source:
                    state["pendingSyncAttempts"] = attempts + 1
                    state["pendingSyncAt"] = (
                        datetime.now(UTC) + timedelta(minutes=5 * (attempts + 1))
                    ).isoformat()
                    state["lastResult"] = "转存已提交，等待网盘文件落盘"
                else:
                    state["pendingSyncAt"] = None
                    state["pendingSyncAttempts"] = 0
                    state["lastResult"] = f"自动同步完成，新增 {new_files} 个文件"
                    state["lastSyncedAt"] = self._iso_now()
                state["lastError"] = ""
                await self._save_states()
                self._log(
                    "info" if waiting_for_source else "success",
                    (
                        f"百度网盘转存等待落盘：{item['name']}"
                        if waiting_for_source
                        else f"百度网盘更新已同步：{item['name']}，新增 {new_files} 个文件"
                    ),
                    copied=copied,
                )
            except Exception as exc:  # noqa: BLE001 - scheduler must retain failure state
                attempts = int(state.get("pendingSyncAttempts") or 0) + 1
                state["pendingSyncAttempts"] = attempts
                if attempts >= 6:
                    state["pendingSyncAt"] = None
                    state["lastResult"] = "自动同步多次失败，等待下次发现更新后再试"
                else:
                    state["pendingSyncAt"] = (
                        datetime.now(UTC) + timedelta(minutes=min(60, 5 * (2 ** min(attempts, 3))))
                    ).isoformat()
                state["lastError"] = str(exc)[:500]
                await self._save_states()
                self._log("error", f"百度网盘自动同步失败：{str(exc)[:300]}", itemId=item_id)
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
        )

    async def _run_requested(self, item_id: str) -> None:
        try:
            if item_id:
                await self.check_item(item_id)
                return
            items = [
                item
                for item in await self.media.list_items()
                if item.get("status") == "ongoing" and item.get("baiduLink")
            ]
            for index, item in enumerate(items):
                try:
                    await self.check_item(item["id"])
                except Exception as exc:  # noqa: BLE001 - continue the requested batch
                    self._log(
                        "warning",
                        f"百度网盘手动检查已跳过 {item['name']}：{str(exc)[:240]}",
                        itemId=item["id"],
                    )
                if index + 1 < len(items):
                    await asyncio.sleep(2)
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
            due = self._parse_time(state.get("nextCheckAt"))
            if due is None or due <= now:
                try:
                    await self.check_item(item["id"])
                except Exception as exc:  # noqa: BLE001 - failure state is stored by check_item
                    self._log("warning", f"百度网盘定时检查已延后：{str(exc)[:300]}")
                return

    async def _cli_status(
        self, *, binary: str | None = None, refresh: bool = False
    ) -> dict[str, Any]:
        loop_time = asyncio.get_running_loop().time()
        selected = str(binary or self.config["binary"])
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
            "binary": self.settings.bdpan_binary,
            "checkIntervalMinutes": self.settings.bdpan_check_interval_minutes,
            "saveRoot": self.cli.normalize_destination(self.settings.bdpan_save_root),
            "settleSeconds": self.settings.bdpan_settle_seconds,
            "maxNewItems": self.settings.bdpan_max_new_items,
        }

    def _normalize_config(self, value: dict[str, Any]) -> dict[str, Any]:
        binary = str(value.get("binary") or self.settings.bdpan_binary).strip()
        if not binary or len(binary) > 500 or "\x00" in binary:
            raise AppError(400, "bdpan 二进制路径格式不正确")
        save_root = self.cli.normalize_destination(
            str(value.get("saveRoot") or self.settings.bdpan_save_root)
        )
        if not save_root:
            raise AppError(400, "请设置百度网盘转存根目录")
        return {
            "enabled": bool(value.get("enabled", False)),
            "binary": binary,
            "checkIntervalMinutes": max(5, min(1440, int(value.get("checkIntervalMinutes") or 10))),
            "saveRoot": save_root,
            "settleSeconds": max(30, min(1800, int(value.get("settleSeconds") or 90))),
            "maxNewItems": max(1, min(100, int(value.get("maxNewItems") or 20))),
        }

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
            prefix = ""
            if len(file.relative_parts) > 1 and not SEASON_DIRECTORY.match(file.relative_parts[0]):
                prefix = file.relative_parts[0]
            grouped.setdefault(prefix, []).append(file)

        candidates: list[dict[str, Any]] = []
        for prefix, grouped_files in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            normalized = cls._files_for_prefix(grouped_files, prefix)
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
                    "totalBytes": sum(max(0, file.size) for file in normalized),
                    "sampleFiles": ["/".join(file.relative_parts) for file in normalized[:5]],
                    "files": normalized,
                }
            )
        return candidates

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

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(UTC).isoformat()

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _log(self, level: str, message: str, **details: Any) -> None:
        self.runtime_logs.add(category="bdpan", level=level, message=message, **details)
