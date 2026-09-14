from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.repositories.media import MediaRepository
from strmflow.schemas.api import MediaItemInput, PublishRequest
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.services.storage import StorageService
from strmflow.utils.paths import (
    join_virtual_path,
    normalize_virtual_path,
    path_base,
    validate_virtual_path,
)


class MediaService:
    def __init__(
        self,
        settings: Settings,
        openlist: OpenListClient,
        storage: StorageService,
        repository: MediaRepository,
        path_config: PathConfigService,
    ) -> None:
        self.settings = settings
        self.openlist = openlist
        self.storage = storage
        self.repository = repository
        self.path_config = path_config

    async def list_items(self) -> list[dict[str, Any]]:
        items = [
            self._normalize(await self._ensure_target_layout(item))
            for item in await self.repository.list()
        ]
        return sorted(filter(None, items), key=lambda item: item["name"].casefold())

    async def get_item(self, item_id: str) -> dict[str, Any]:
        item = await self.repository.get(item_id)
        if not item:
            raise AppError(404, "媒体配置不存在")
        item = await self._ensure_target_layout(item)
        return self._normalize(item) or item

    async def save_item(self, body: MediaItemInput) -> dict[str, Any]:
        source_path = validate_virtual_path(body.source_path, "源目录")
        duplicate = await self.repository.get_by_source_path(source_path)
        if duplicate and duplicate.get("id") != body.id:
            raise AppError(409, f"该媒体已添加：{duplicate.get('name') or source_path}")
        target = self._build_target(body.model_dump(by_alias=False))
        generated_path = normalize_virtual_path(body.generated_path or source_path)

        item_id = body.id or "m" + hashlib.sha256(source_path.encode()).hexdigest()[:8]
        existing = await self.repository.get(item_id)
        same_paths = bool(
            existing
            and existing.get("generatedPath") == generated_path
            and existing.get("targetDir") == target["targetDir"]
        )
        now = datetime.now(UTC).isoformat()
        item = {
            "id": item_id,
            "name": target["folderName"],
            "sourcePath": source_path,
            "generatedPath": generated_path,
            "targetDir": target["targetDir"],
            "title": target["title"],
            "year": target["year"],
            "category": target["category"],
            "mediaType": target["mediaType"],
            "status": body.status,
            "totalEpisodes": str(body.total_episodes or ""),
            "season": body.season,
            "updateSchedule": body.update_schedule.strip(),
            "baiduLink": body.baidu_link.strip(),
            "syncedFiles": existing.get("syncedFiles", []) if same_paths else [],
            "lastSyncedAt": existing.get("lastSyncedAt") if same_paths else None,
            "createdAt": existing.get("createdAt", now) if existing else now,
            "updatedAt": now,
        }
        item = await self.repository.upsert(item)
        return self._normalize(item) or item

    async def delete_item(self, item_id: str) -> None:
        await self.repository.delete(item_id)

    async def update_item(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        item = await self.repository.update(item_id, patch)
        return self._normalize(item) or item

    async def rebase_target_root(self, root: str) -> int:
        updated = 0
        for item in await self.repository.list():
            target_dir = self._target_directory(
                root,
                item.get("mediaType") or "tv",
                item.get("category") or "未分类",
                item.get("name") or item.get("title") or "Media",
            )
            if target_dir == item.get("targetDir"):
                continue
            await self.repository.update(item["id"], {"targetDir": target_dir, "syncedFiles": []})
            updated += 1
        return updated

    async def _ensure_target_layout(self, item: dict[str, Any]) -> dict[str, Any]:
        expected = self._target_directory(
            self.path_config.emby_strm_root,
            str(item.get("mediaType") or "tv"),
            str(item.get("category") or "未分类"),
            str(item.get("name") or item.get("title") or "Media"),
        )
        if normalize_virtual_path(item.get("targetDir")) == expected:
            return item
        return await self.repository.update(item["id"], {"targetDir": expected, "syncedFiles": []})

    async def collect_files(
        self,
        root: str,
        *,
        tolerant: bool = False,
        skip_empty_strm: bool = False,
    ) -> list[str]:
        files: list[str] = []

        async def walk(absolute: str, relative: str, depth: int) -> None:
            if depth > 12 or len(files) > 10_000:
                return
            for entry in await self.openlist.list_dir(absolute, refresh=True):
                name = entry.get("name")
                if not isinstance(name, str):
                    continue
                child = f"{relative}/{name}".strip("/")
                if entry.get("is_dir") is True:
                    await walk(join_virtual_path(absolute, name), child, depth + 1)
                else:
                    if (
                        skip_empty_strm
                        and child.casefold().endswith(".strm")
                        and int(entry.get("size") or 0) <= 0
                    ):
                        continue
                    files.append(child)

        try:
            await walk(normalize_virtual_path(root), "", 0)
        except AppError:
            if not tolerant:
                raise
        return sorted(files, key=str.casefold)

    async def preview_publish(self, body: PublishRequest) -> dict[str, Any]:
        context, source, target = await self._resolve_publish(body)
        files = await self.collect_files(source)
        if not files:
            raise AppError(409, f"本地 STRM 目录为空，请先扫描生成：{source}")
        targets = set(await self.collect_files(target, tolerant=True, skip_empty_strm=True))
        plan = self._build_plan(files, context, body.rename_plan)
        seasons = self._effective_seasons(files, context)
        missing = [entry for entry in plan if entry["targetRel"] not in targets]
        synced = set(context.get("syncedFiles") or [])
        return {
            **{key: context.get(key) for key in ("id", "name") if context.get(key)},
            "sourcePath": context.get("sourcePath", source),
            "generatedPath": source,
            "targetDir": target,
            "totalFiles": len(files),
            "episodeCount": sum(name.lower().endswith(".strm") for name in files),
            "totalEpisodes": context.get("totalEpisodes", ""),
            "seasons": seasons,
            "seasonCount": len(seasons),
            "newFiles": [name for name in files if name not in synced],
            "missingTargetFiles": [entry["sourceRel"] for entry in missing],
            "pendingFiles": len(missing),
            "plan": [
                {**entry, "changed": entry["sourceRel"] != entry["targetRel"]} for entry in missing
            ],
        }

    async def publish(self, body: PublishRequest) -> dict[str, Any]:
        context, source, target = await self._resolve_publish(body)
        files = await self.collect_files(source)
        if not files:
            raise AppError(409, f"本地 STRM 目录为空，请先扫描生成：{source}")
        target_files = set(await self.collect_files(target, tolerant=True, skip_empty_strm=True))
        plan = self._build_plan(files, context, body.rename_plan)
        seasons = self._effective_seasons(files, context)
        missing = [entry for entry in plan if entry["targetRel"] not in target_files]
        synced = set(context.get("syncedFiles") or [])
        new_files = [name for name in files if name not in synced]
        missing_target_files = [entry["sourceRel"] for entry in missing]
        await self.openlist.mkdir(target)
        copied, renamed = await self._copy_plan(source, target, missing)
        episode_count = sum(name.lower().endswith(".strm") for name in files)
        auto_completed = False
        status = context.get("status", "ongoing")
        if context.get("id"):
            total = int(context.get("totalEpisodes") or 0)
            auto_completed = total > 0 and episode_count >= total and status != "completed"
            status = "completed" if total > 0 and episode_count >= total else status
            await self.update_item(
                context["id"],
                {
                    "syncedFiles": files,
                    "lastSyncedAt": datetime.now(UTC).isoformat(),
                    "status": status,
                },
            )
        return {
            **{key: context.get(key) for key in ("id", "name") if context.get(key)},
            "sourcePath": context.get("sourcePath", source),
            "generatedPath": source,
            "targetDir": target,
            "copied": copied,
            "newFiles": new_files,
            "missingTargetFiles": missing_target_files,
            "totalFiles": len(files),
            "episodeCount": episode_count,
            "seasons": seasons,
            "seasonCount": len(seasons),
            "status": status,
            "autoCompleted": auto_completed,
            "refreshed": False,
            "renamedFiles": renamed,
        }

    async def _resolve_publish(self, body: PublishRequest) -> tuple[dict[str, Any], str, str]:
        if body.id:
            item = await self.get_item(body.id)
            return item, item["generatedPath"], item["targetDir"]
        folders = await self.storage.list_media_folders(False)
        folder = next(
            (
                item
                for item in folders
                if (
                    body.scan_path is None
                    or item["scanPath"] == normalize_virtual_path(body.scan_path)
                )
                and (
                    body.media_path is None
                    or item["mediaPath"] == normalize_virtual_path(body.media_path)
                )
                and (
                    body.scan_path is not None
                    or body.media_path is not None
                    or item["name"] == body.name
                )
            ),
            None,
        )
        if not folder or (body.name and folder["name"] != body.name):
            raise AppError(404, "媒体目录不存在或已被移除")
        target = self._build_target(body.model_dump(by_alias=False))
        return (
            {**folder, **target, "name": folder["name"]},
            folder["mediaPath"],
            target["targetDir"],
        )

    async def _copy_plan(
        self, source_root: str, target_root: str, plan: list[dict[str, str]]
    ) -> tuple[int, list[dict[str, str]]]:
        groups: dict[tuple[str, str], list[dict[str, str]]] = {}
        for entry in plan:
            source_dir = entry["sourceRel"].rsplit("/", 1)[0] if "/" in entry["sourceRel"] else ""
            target_dir = entry["targetRel"].rsplit("/", 1)[0] if "/" in entry["targetRel"] else ""
            groups.setdefault((source_dir, target_dir), []).append(entry)
        renamed: list[dict[str, str]] = []
        for (source_dir, target_dir), entries in groups.items():
            await self._ensure_relative_dirs(target_root, target_dir)
            source_directory = join_virtual_path(source_root, source_dir)
            target_directory = join_virtual_path(target_root, target_dir)
            copy_entries: list[dict[str, str]] = []
            for entry in entries:
                source_name = entry["sourceRel"].rsplit("/", 1)[-1]
                target_name = entry["targetRel"].rsplit("/", 1)[-1]
                if source_name.casefold().endswith(".strm"):
                    info = await self.openlist.get_file_info(
                        join_virtual_path(source_directory, source_name)
                    )
                    raw_url = str(info.get("raw_url") or "").strip()
                    if str(info.get("provider") or "").casefold() == "strm" and raw_url:
                        await self.openlist.write_text(
                            join_virtual_path(target_directory, target_name), raw_url
                        )
                        if source_name != target_name:
                            renamed.append({"from": entry["sourceRel"], "to": entry["targetRel"]})
                        continue
                copy_entries.append(entry)
            if not copy_entries:
                continue
            await self.openlist.copy(
                source_directory,
                target_directory,
                list(
                    dict.fromkeys(entry["sourceRel"].rsplit("/", 1)[-1] for entry in copy_entries)
                ),
            )
            changes = [
                {
                    "src_name": entry["sourceRel"].rsplit("/", 1)[-1],
                    "new_name": entry["targetRel"].rsplit("/", 1)[-1],
                }
                for entry in copy_entries
                if entry["sourceRel"].rsplit("/", 1)[-1] != entry["targetRel"].rsplit("/", 1)[-1]
            ]
            if changes:
                await self.openlist.batch_rename(target_directory, changes)
                renamed.extend(
                    {"from": entry["sourceRel"], "to": entry["targetRel"]}
                    for entry in copy_entries
                    if entry["sourceRel"].rsplit("/", 1)[-1]
                    != entry["targetRel"].rsplit("/", 1)[-1]
                )
        return len(plan), renamed

    async def _ensure_relative_dirs(self, root: str, relative: str) -> None:
        current = root
        for segment in filter(None, relative.split("/")):
            current = join_virtual_path(current, segment)
            await self.openlist.mkdir(current)

    def _build_plan(
        self,
        files: list[str],
        context: dict[str, Any],
        rename_plan: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        overrides: dict[str, str] = {}
        for row in rename_plan or []:
            source = str(row.get("sourceRel") or row.get("source") or row.get("from") or "")
            target = str(row.get("targetRel") or row.get("target") or row.get("to") or "")
            if not source or not target or target.startswith("/") or ".." in target.split("/"):
                raise AppError(400, "重命名列表不合法")
            overrides[source] = target
        if any(source not in files for source in overrides):
            raise AppError(400, "重命名源文件不存在")
        normalization_context = {
            **context,
            "autoMultiSeason": len(self._explicit_seasons(files)) > 1,
        }
        used: set[str] = set()
        plan = []
        for source in files:
            target = overrides.get(source) or self._normalized_target_name(
                source, normalization_context
            )
            candidate = target
            stem, dot, extension = candidate.rpartition(".")
            counter = 2
            while candidate in used:
                candidate = (
                    f"{stem or target} - {counter}{dot}{extension}"
                    if dot
                    else f"{target} - {counter}"
                )
                counter += 1
            used.add(candidate)
            plan.append({"sourceRel": source, "targetRel": candidate})
        return plan

    @classmethod
    def _normalized_target_name(cls, source: str, context: dict[str, Any]) -> str:
        if context.get("mediaType") != "tv":
            return source
        normalized_source = source.replace("\\", "/").strip("/")
        _, _, name = normalized_source.rpartition("/")
        detected_season, episode = cls._source_season_episode(normalized_source)
        configured_season = int(context.get("season") or 1)
        season = (
            detected_season
            if context.get("autoMultiSeason") and detected_season is not None
            else configured_season
        )

        if not normalized_source.lower().endswith(".strm"):
            return cls._normalize_season_directory(normalized_source, season)

        if episode is None:
            return f"Season {season:02d}/{name}"
        series = context.get("name") or context.get("folderName") or context.get("title") or "Media"
        filename = f"{series} - S{season:02d}E{episode:02d}.strm"
        return f"Season {season:02d}/{filename}"

    @classmethod
    def _explicit_seasons(cls, files: list[str]) -> set[int]:
        seasons: set[int] = set()
        for source in files:
            if not source.casefold().endswith(".strm"):
                continue
            season, _ = cls._source_season_episode(source)
            if season is not None:
                seasons.add(season)
        return seasons

    @classmethod
    def _effective_seasons(cls, files: list[str], context: dict[str, Any]) -> list[int]:
        explicit = cls._explicit_seasons(files)
        if len(explicit) > 1:
            return sorted(explicit)
        return [int(context.get("season") or 1)] if context.get("mediaType") == "tv" else []

    @staticmethod
    def _source_season_episode(source: str) -> tuple[int | None, int | None]:
        normalized = unquote(source.replace("\\", "/").strip("/"))
        *directories, filename = normalized.split("/")
        directory_season: int | None = None
        for segment in reversed(directories):
            match = re.match(
                r"(?i)^\s*(?:season[ ._-]*0*(\d{1,2})|s0*(\d{1,2})(?:\b|[ ._-])|第\s*0*(\d{1,2})\s*季)",
                segment,
            )
            if match:
                directory_season = int(next(group for group in match.groups() if group is not None))
                break

        base = filename[:-5] if filename.casefold().endswith(".strm") else filename
        season_episode = re.search(
            r"(?i)(?:Season[ ._-]*0*(\d{1,2})[ ._-]+Episode[ ._-]*0*(\d{1,4})|"
            r"(?:^|[ ._\-()[\]【】])S0*(\d{1,2})[ ._-]*E(?:P)?0*(\d{1,4})"
            r"(?=$|[ ._\-()[\]【】])|第\s*0*(\d{1,2})\s*季.*?第\s*0*(\d{1,4})\s*[集话話]|"
            r"(?<!\d)(\d{1,2})x0*(\d{1,4})(?!\d))",
            base,
        )
        filename_season: int | None = None
        episode: int | None = None
        if season_episode:
            groups = season_episode.groups()
            for index in range(0, len(groups), 2):
                if groups[index] is not None:
                    filename_season = int(groups[index])
                    episode = int(groups[index + 1])
                    break
        if episode is None:
            episode_match = re.search(
                r"(?i)(?:第\s*0*(\d{1,4})\s*[集话話]|"
                r"(?:^|[ ._\-()[\]【】])(?:EP|E)0*(\d{1,4})(?=$|[ ._\-()[\]【】])|"
                r"^\s*0*(\d{1,4})(?=$|[ ._\-()[\]【】]))",
                base,
            )
            if episode_match:
                episode = int(next(group for group in episode_match.groups() if group is not None))
        return directory_season if directory_season is not None else filename_season, episode

    @staticmethod
    def _normalize_season_directory(source: str, season: int) -> str:
        parts = source.split("/")
        for index, segment in enumerate(parts[:-1]):
            if re.match(
                r"(?i)^\s*(?:season[ ._-]*0*\d{1,2}|s0*\d{1,2}(?:\b|[ ._-])|第\s*0*\d{1,2}\s*季)",
                unquote(segment),
            ):
                return "/".join([f"Season {season:02d}", *parts[index + 1 :]])
        return source

    def _build_target(self, body: dict[str, Any]) -> dict[str, Any]:
        title = self._validate_segment(body.get("title"), "剧名", 100)
        year = str(body.get("year") or "").strip()
        if year and not re.fullmatch(r"\d{4}", year):
            raise AppError(400, "年份必须是 4 位数字")
        media_type = self._validate_segment(
            body.get("media_type") or body.get("mediaType") or "tv", "媒体类型", 50
        )
        category = self._validate_segment(body.get("category") or "未分类", "分类", 50)
        folder_name = f"{title} ({year})" if year else title
        season = int(body.get("season") or 1)
        return {
            "targetDir": self._target_directory(
                self.path_config.emby_strm_root,
                media_type,
                category,
                folder_name,
            ),
            "folderName": folder_name,
            "mediaType": media_type,
            "category": category,
            "title": title,
            "year": year,
            "season": season,
        }

    @staticmethod
    def _target_directory(
        root: str,
        media_type: str,
        category: str,
        folder_name: str,
    ) -> str:
        return join_virtual_path(root, media_type, category, folder_name)

    @staticmethod
    def _validate_segment(value: object, label: str, limit: int) -> str:
        text = str(value or "").strip()
        if not text or len(text) > limit or any(char in text for char in "/\\\x00"):
            raise AppError(400, f"{label}不合法")
        return text

    @classmethod
    def _normalize(cls, item: object) -> dict[str, Any] | None:
        if (
            not isinstance(item, dict)
            or not item.get("id")
            or not item.get("sourcePath")
            or not item.get("targetDir")
        ):
            return None
        synced_files = item.get("syncedFiles") if isinstance(item.get("syncedFiles"), list) else []
        season = int(item.get("season") or 1)
        return {
            **item,
            "name": item.get("name") or path_base(item["targetDir"]),
            "scanPath": normalize_virtual_path(item["sourcePath"]),
            "mediaPath": normalize_virtual_path(item["targetDir"]),
            "sourcePath": normalize_virtual_path(item["sourcePath"]),
            "generatedPath": normalize_virtual_path(
                item.get("generatedPath") or item["sourcePath"]
            ),
            "targetDir": normalize_virtual_path(item["targetDir"]),
            "syncedFiles": synced_files,
            "status": "completed" if item.get("status") == "completed" else "ongoing",
            "totalEpisodes": item.get("totalEpisodes") or "",
            "season": season,
            "seasons": cls._effective_seasons(synced_files, {**item, "season": season}),
            "updateSchedule": item.get("updateSchedule") or "",
            "baiduLink": item.get("baiduLink") or "",
        }
