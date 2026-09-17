from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.media_layout import (
    canonical_type_dir,
    media_resource_path,
    normalize_category,
    type_dir_from_source,
)
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.repositories.media import MediaRepository
from strmflow.schemas.api import MediaItemInput, PublishRequest
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.services.storage import StorageService
from strmflow.utils.episodes import (
    media_quality_label,
    media_quality_rank,
    select_preferred_episodes,
    source_season_episode,
)
from strmflow.utils.paths import (
    join_virtual_path,
    normalize_virtual_path,
    path_base,
    validate_virtual_path,
)

# Version 2 rewrites manifests created while an Emby target overlapped the
# physical media source. Those files can still contain the old unclassified URL
# even after the real videos have been restored.
STRM_MANIFEST_VERSION = 2


class MediaService:
    def __init__(
        self,
        settings: Settings,
        openlist: OpenListClient,
        storage: StorageService,
        repository: MediaRepository,
        path_config: PathConfigService,
        runtime_logs: RuntimeLogStore | None = None,
    ) -> None:
        self.settings = settings
        self.openlist = openlist
        self.storage = storage
        self.repository = repository
        self.path_config = path_config
        self.runtime_logs = runtime_logs

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
            "manifestVersion": int(existing.get("manifestVersion") or 0)
            if same_paths and existing
            else 0,
            "lastSyncedAt": existing.get("lastSyncedAt") if same_paths else None,
            "createdAt": existing.get("createdAt", now) if existing else now,
            "updatedAt": now,
        }
        item = await self.repository.upsert(item)
        result = self._normalize(item) or item
        self._log(
            "success",
            f"媒体配置已{'更新' if existing else '添加'}：{result['name']}",
            itemId=item_id,
            sourcePath=source_path,
            targetPath=result["targetDir"],
            status=result.get("status"),
            season=result.get("season"),
        )
        return result

    async def delete_item(self, item_id: str) -> None:
        item = await self.repository.get(item_id)
        await self.repository.delete(item_id)
        self._log(
            "success",
            f"媒体配置已删除：{(item or {}).get('name') or item_id}",
            itemId=item_id,
        )

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
                source_path=str(item.get("sourcePath") or ""),
            )
            if target_dir == item.get("targetDir"):
                continue
            await self.repository.update(
                item["id"],
                {"targetDir": target_dir, "syncedFiles": [], "manifestVersion": 0},
            )
            updated += 1
        self._log(
            "success",
            "媒体目标根目录映射已更新",
            targetRoot=root,
            updatedItemCount=updated,
        )
        return updated

    async def _ensure_target_layout(self, item: dict[str, Any]) -> dict[str, Any]:
        expected = self._target_directory(
            self.path_config.emby_strm_root,
            str(item.get("mediaType") or "tv"),
            str(item.get("category") or "未分类"),
            str(item.get("name") or item.get("title") or "Media"),
            source_path=str(item.get("sourcePath") or ""),
        )
        if normalize_virtual_path(item.get("targetDir")) == expected:
            return item
        return await self.repository.update(
            item["id"],
            {"targetDir": expected, "syncedFiles": [], "manifestVersion": 0},
        )

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
        self._log(
            "info",
            f"开始生成同步预览：{context.get('name') or source}",
            itemId=context.get("id") or "",
            sourcePath=source,
            targetPath=target,
        )
        discovered_files = await self.collect_files(source)
        if not discovered_files:
            raise AppError(409, f"本地 STRM 目录为空，请先扫描生成：{source}")
        files, duplicate_files = self._preferred_media_files(discovered_files, context)
        targets = set(await self.collect_files(target, tolerant=True, skip_empty_strm=True))
        plan = self._build_plan(files, context, body.rename_plan)
        plan, quality_upgrades = self._apply_version_targets(plan, context, targets)
        seasons = self._effective_seasons(files, context)
        missing = [entry for entry in plan if entry["targetRel"] not in targets]
        synced = set(context.get("syncedFiles") or [])
        self._log(
            "info",
            f"同步预览生成完成：{context.get('name') or source}",
            sourceFileCount=len(files),
            targetFileCount=len(targets),
            pendingFileCount=len(missing),
            seasonCount=len(seasons),
            skippedDuplicateCount=len(duplicate_files),
            qualityUpgradeCount=len(quality_upgrades),
        )
        return {
            **{key: context.get(key) for key in ("id", "name") if context.get(key)},
            "sourcePath": context.get("sourcePath", source),
            "generatedPath": source,
            "targetDir": target,
            "totalFiles": len(files),
            "episodeCount": self._episode_count(files, context),
            "totalEpisodes": context.get("totalEpisodes", ""),
            "seasons": seasons,
            "seasonCount": len(seasons),
            "newFiles": [name for name in files if name not in synced],
            "missingTargetFiles": [entry["sourceRel"] for entry in missing],
            "pendingFiles": len(missing),
            "qualityUpgrades": quality_upgrades,
            "qualityUpgradeCount": len(quality_upgrades),
            "skippedDuplicateFiles": duplicate_files,
            "skippedDuplicateCount": len(duplicate_files),
            "plan": [
                {**entry, "changed": entry["sourceRel"] != entry["targetRel"]} for entry in missing
            ],
        }

    async def publish(self, body: PublishRequest) -> dict[str, Any]:
        context, source, target = await self._resolve_publish(body)
        await self.storage.assert_publish_target_isolated(source, target)
        self._log(
            "info",
            f"开始同步媒体：{context.get('name') or source}",
            itemId=context.get("id") or "",
            sourcePath=source,
            targetPath=target,
        )
        discovered_files = await self.collect_files(source)
        if not discovered_files:
            raise AppError(409, f"本地 STRM 目录为空，请先扫描生成：{source}")
        files, duplicate_files = self._preferred_media_files(discovered_files, context)
        all_target_files = await self.collect_files(target, tolerant=True)
        target_file_list = await self.collect_files(target, tolerant=True, skip_empty_strm=True)
        target_duplicates = self._legacy_target_duplicates(all_target_files, context)
        if target_duplicates:
            await self._remove_relative_files(target, target_duplicates)
            self._log(
                "success",
                f"已清理目标目录中的重复剧集：{context.get('name') or source}",
                removedCount=len(target_duplicates),
                removedFiles=target_duplicates[:20],
            )
        target_files = set(target_file_list) - set(target_duplicates)
        plan = self._build_plan(files, context, body.rename_plan)
        plan, quality_upgrades = self._apply_version_targets(plan, context, target_files)
        seasons = self._effective_seasons(files, context)
        missing = [entry for entry in plan if entry["targetRel"] not in target_files]
        normalize_manifests = int(context.get("manifestVersion") or 0) < STRM_MANIFEST_VERSION
        legacy_manifests = (
            [
                entry
                for entry in plan
                if entry["targetRel"] in target_files
                and entry["targetRel"].casefold().endswith(".strm")
            ]
            if normalize_manifests
            else []
        )
        synced = set(context.get("syncedFiles") or [])
        sync_plan = list(
            {
                (entry["sourceRel"], entry["targetRel"]): entry
                for entry in [*missing, *legacy_manifests]
            }.values()
        )
        new_files = [name for name in files if name not in synced]
        new_episode_identities = self._new_episode_identities(files, list(synced), context)
        new_episode_count = len(new_episode_identities)
        missing_target_files = [entry["sourceRel"] for entry in missing]
        warmup_sources = (
            set(new_files)
            | set(missing_target_files)
            | {entry["sourceRel"] for entry in legacy_manifests}
        )
        warmup_paths = [
            join_virtual_path(target, entry["targetRel"])
            for entry in plan
            if entry["sourceRel"] in warmup_sources
            and entry["targetRel"].casefold().endswith(".strm")
        ]
        self._log(
            "info",
            f"媒体文件清点完成：{context.get('name') or source}",
            sourceFileCount=len(files),
            existingTargetCount=len(target_files),
            pendingFileCount=len(missing),
            qualityUpgradeCount=len(quality_upgrades),
            manifestUpgradeCount=len(legacy_manifests),
            seasons=seasons,
            skippedDuplicateCount=len(duplicate_files),
            removedTargetDuplicateCount=len(target_duplicates),
        )
        await self.openlist.mkdir(target)
        copied, renamed = await self._copy_plan(source, target, sync_plan)
        episode_count = self._episode_count(files, context)
        auto_completed = False
        status = context.get("status", "ongoing")
        if context.get("id"):
            total = int(context.get("totalEpisodes") or 0)
            auto_completed = total > 0 and episode_count >= total and status != "completed"
            status = "completed" if total > 0 and episode_count >= total else status
            await self.update_item(
                context["id"],
                {
                    "syncedFiles": self._merge_synced_inventory(files, context),
                    "lastSyncedAt": datetime.now(UTC).isoformat(),
                    "status": status,
                    "manifestVersion": STRM_MANIFEST_VERSION,
                },
            )
        self._log(
            "success",
            f"媒体同步完成：{context.get('name') or source}",
            itemId=context.get("id") or "",
            copiedCount=copied,
            newFileCount=len(new_files),
            episodeCount=episode_count,
            renamedCount=len(renamed),
            qualityUpgradeCount=len(quality_upgrades),
            manifestUpgradeCount=len(legacy_manifests),
            status=status,
            autoCompleted=auto_completed,
            skippedDuplicateCount=len(duplicate_files),
        )
        return {
            **{key: context.get(key) for key in ("id", "name") if context.get(key)},
            "sourcePath": context.get("sourcePath", source),
            "generatedPath": source,
            "targetDir": target,
            "copied": copied,
            "newFiles": new_files,
            "newEpisodeCount": new_episode_count,
            "newEpisodes": [
                f"S{season:02d}E{episode:02d}" for season, episode in new_episode_identities
            ],
            "missingTargetFiles": missing_target_files,
            "warmupPaths": warmup_paths,
            "totalFiles": len(files),
            "episodeCount": episode_count,
            "seasons": seasons,
            "seasonCount": len(seasons),
            "status": status,
            "autoCompleted": auto_completed,
            "refreshed": False,
            "renamedFiles": renamed,
            "normalizedStrmFiles": len(legacy_manifests),
            "qualityUpgrades": quality_upgrades,
            "qualityUpgradeCount": len(quality_upgrades),
            "replacedStrmFiles": 0,
            "skippedDuplicateFiles": duplicate_files,
            "skippedDuplicateCount": len(duplicate_files),
            "removedTargetDuplicateFiles": target_duplicates,
            "removedTargetDuplicateCount": len(target_duplicates),
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
            self._log(
                "info",
                "正在同步 STRM 文件组",
                sourceDirectory=source_directory,
                targetDirectory=target_directory,
                fileCount=len(entries),
            )
            strm_entries = [
                entry for entry in entries if entry["sourceRel"].casefold().endswith(".strm")
            ]
            other_entries = [entry for entry in entries if entry not in strm_entries]

            # Use the same OpenList-side copy flow as the demo. For a Strm
            # storage this materializes the generated manifest itself, whereas
            # persisting fs/get.raw_url would create an extra .strm -> .strm
            # hop and leave Emby without the real container and duration.
            #
            # Copy STRM files one at a time: some OpenList versions return zero
            # byte placeholders when several virtual Strm objects are copied in
            # one request. A single-file copy is synchronous and reliable.
            for entry in strm_entries:
                source_name = entry["sourceRel"].rsplit("/", 1)[-1]
                target_name = entry["targetRel"].rsplit("/", 1)[-1]
                copied_path = join_virtual_path(target_directory, source_name)
                await self.openlist.copy(source_directory, target_directory, [source_name])
                if not await self._wait_for_materialized_strm(copied_path):
                    source_path = join_virtual_path(source_directory, source_name)
                    try:
                        manifest = await self.openlist.read_text(source_path)
                    except AppError as exc:
                        await self.openlist.remove(target_directory, [source_name])
                        raise AppError(
                            502,
                            f"源 STRM 暂时无法生成，请稍后重新扫描同步：{source_path}",
                        ) from exc
                    media_url = next(
                        (
                            line.strip()
                            for line in manifest.lstrip("\ufeff").splitlines()
                            if line.strip().startswith(("http://", "https://"))
                        ),
                        "",
                    )
                    if not media_url:
                        await self.openlist.remove(target_directory, [source_name])
                        raise AppError(502, f"源 STRM 内容无效：{source_path}")
                    await self.openlist.write_text(copied_path, media_url + "\n")
                    if not await self._wait_for_materialized_strm(copied_path):
                        await self.openlist.remove(target_directory, [source_name])
                        raise AppError(502, f"OpenList 未能写入有效 STRM：{copied_path}")
                    self._log(
                        "warning",
                        "OpenList 复制返回空 STRM，已从源清单恢复内容",
                        sourcePath=source_path,
                        targetPath=copied_path,
                    )
                if source_name != target_name:
                    await self.openlist.batch_rename(
                        target_directory,
                        [{"src_name": source_name, "new_name": target_name}],
                    )
                    renamed.append({"from": entry["sourceRel"], "to": entry["targetRel"]})

            if not other_entries:
                continue
            await self.openlist.copy(
                source_directory,
                target_directory,
                list(
                    dict.fromkeys(entry["sourceRel"].rsplit("/", 1)[-1] for entry in other_entries)
                ),
            )
            changes = [
                {
                    "src_name": entry["sourceRel"].rsplit("/", 1)[-1],
                    "new_name": entry["targetRel"].rsplit("/", 1)[-1],
                }
                for entry in other_entries
                if entry["sourceRel"].rsplit("/", 1)[-1] != entry["targetRel"].rsplit("/", 1)[-1]
            ]
            if changes:
                await self.openlist.batch_rename(target_directory, changes)
                renamed.extend(
                    {"from": entry["sourceRel"], "to": entry["targetRel"]}
                    for entry in other_entries
                    if entry["sourceRel"].rsplit("/", 1)[-1]
                    != entry["targetRel"].rsplit("/", 1)[-1]
                )
        return len(plan), renamed

    async def _wait_for_materialized_strm(self, path: str) -> bool:
        for delay in (0.0, 0.15, 0.35):
            if delay:
                await asyncio.sleep(delay)
            try:
                info = await self.openlist.get_file_info(path)
            except AppError:
                # The copy endpoint can return just before a Strm driver has
                # published the generated object into the destination listing.
                continue
            if int(info.get("size") or 0) > 0:
                return True
        return False

    def _log(self, level: str, message: str, **details: Any) -> None:
        if self.runtime_logs:
            self.runtime_logs.add(category="media", level=level, message=message, **details)

    async def _ensure_relative_dirs(self, root: str, relative: str) -> None:
        current = root
        for segment in filter(None, relative.split("/")):
            current = join_virtual_path(current, segment)
            await self.openlist.mkdir(current)

    async def _remove_relative_files(self, root: str, files: list[str]) -> None:
        groups: dict[str, list[str]] = {}
        for relative in files:
            directory, separator, name = relative.rpartition("/")
            groups.setdefault(directory if separator else "", []).append(name or relative)
        for directory, names in groups.items():
            await self.openlist.remove(join_virtual_path(root, directory), names)

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

    def _apply_version_targets(
        self,
        plan: list[dict[str, str]],
        context: dict[str, Any],
        target_files: set[str],
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Keep the first selected encode canonical and publish only later upgrades as versions."""
        if context.get("mediaType") != "tv":
            return plan, []
        previous_files = list(map(str, context.get("syncedFiles") or []))
        version_context = {
            **context,
            "autoMultiSeason": len(
                self._explicit_seasons([*previous_files, *(entry["sourceRel"] for entry in plan)])
            )
            > 1,
        }
        previous = self._best_episode_sources(previous_files, version_context)
        result: list[dict[str, str]] = []
        upgrades: list[dict[str, str]] = []
        for entry in plan:
            source = entry["sourceRel"]
            requested_target = entry["targetRel"]
            if not source.casefold().endswith(".strm"):
                result.append(entry)
                continue
            canonical = self._normalized_target_name(source, version_context)
            identity = self._target_episode_identity(canonical, version_context)
            prior = previous.get(identity) if identity else None
            # A deleted canonical file is restored under its stable name. This also
            # covers existing libraries imported before quality history was recorded.
            if not prior or canonical not in target_files:
                result.append(entry)
                continue
            version_target = self._version_target_name(canonical, source)
            if source == prior:
                result.append(
                    {**entry, "targetRel": version_target}
                    if version_target in target_files
                    else entry
                )
                continue
            if media_quality_rank(source) <= media_quality_rank(prior):
                # Never replace a published episode with an equal or lower quality
                # duplicate. The existing canonical/version file remains untouched.
                result.append(entry)
                continue
            target = requested_target if requested_target != canonical else version_target
            version_entry = {**entry, "targetRel": target}
            result.append(version_entry)
            if target not in target_files:
                upgrades.append(
                    {
                        "sourceRel": source,
                        "targetRel": target,
                        "from": media_quality_label(prior),
                        "to": media_quality_label(source),
                    }
                )
        return result, upgrades

    @classmethod
    def _best_episode_sources(
        cls, files: list[str], context: dict[str, Any]
    ) -> dict[tuple[int, int], str]:
        best: dict[tuple[int, int], str] = {}
        for source in files:
            if not str(source).casefold().endswith(".strm"):
                continue
            canonical = cls._normalized_target_name(str(source), context)
            identity = cls._target_episode_identity(canonical, context)
            if identity is None:
                continue
            current = best.get(identity)
            if current is None or media_quality_rank(str(source)) > media_quality_rank(current):
                best[identity] = str(source)
        return best

    @classmethod
    def _merge_synced_inventory(cls, files: list[str], context: dict[str, Any]) -> list[str]:
        """Retain the best known source per episode as the quality-upgrade baseline."""
        if context.get("mediaType") != "tv":
            return files
        merged = list(dict.fromkeys([*map(str, context.get("syncedFiles") or []), *files]))
        preferred, duplicates = cls._preferred_media_files(merged, context)
        duplicate_set = set(duplicates)
        # Non-episode assets describe the current source tree, not historical state.
        current_non_episodes = {
            value
            for value in files
            if not value.casefold().endswith(".strm") or source_season_episode(value)[1] is None
        }
        return sorted(
            dict.fromkeys(
                value
                for value in preferred
                if value not in duplicate_set
                and (
                    value.casefold().endswith(".strm")
                    and source_season_episode(value)[1] is not None
                    or value in current_non_episodes
                )
            ),
            key=str.casefold,
        )

    @classmethod
    def _new_episode_count(
        cls, files: list[str], previous: list[str], context: dict[str, Any]
    ) -> int:
        return len(cls._new_episode_identities(files, previous, context))

    @classmethod
    def _new_episode_identities(
        cls, files: list[str], previous: list[str], context: dict[str, Any]
    ) -> list[tuple[int, int]]:
        identity_context = {
            **context,
            "autoMultiSeason": len(cls._explicit_seasons([*files, *previous])) > 1,
        }
        previous_ids = {
            identity
            for source in previous
            if source.casefold().endswith(".strm")
            if (
                identity := cls._target_episode_identity(
                    cls._normalized_target_name(source, identity_context), identity_context
                )
            )
        }
        current_ids = {
            identity
            for source in files
            if source.casefold().endswith(".strm")
            if (
                identity := cls._target_episode_identity(
                    cls._normalized_target_name(source, identity_context), identity_context
                )
            )
        }
        return sorted(current_ids - previous_ids)

    @staticmethod
    def _target_episode_identity(target: str, context: dict[str, Any]) -> tuple[int, int] | None:
        season, episode = source_season_episode(target)
        if episode is None:
            return None
        return season or int(context.get("season") or 1), episode

    @staticmethod
    def _version_target_name(canonical: str, source: str) -> str:
        if not canonical.casefold().endswith(".strm"):
            return canonical
        return f"{canonical[:-5]} - {media_quality_label(source)}.strm"

    @classmethod
    def _legacy_target_duplicates(cls, files: list[str], context: dict[str, Any]) -> list[str]:
        normalized_by_id: dict[tuple[int, int], str] = {}
        for name in files:
            identity = cls._target_episode_identity(name, context)
            if identity is None:
                continue
            season, episode = identity
            series = context.get("name") or context.get("title") or "Media"
            canonical = f"Season {season:02d}/{series} - S{season:02d}E{episode:02d}.strm"
            if canonical in files:
                normalized_by_id[identity] = canonical
        duplicates: list[str] = []
        for name in files:
            identity = cls._target_episode_identity(name, context)
            canonical = normalized_by_id.get(identity) if identity else None
            if not canonical or name == canonical:
                continue
            version_prefix = canonical[:-5] + " - "
            if not (name.startswith(version_prefix) and name.casefold().endswith(".strm")):
                duplicates.append(name)
                continue
            if re.search(r"\s-\s\d+\.strm$", name, re.IGNORECASE):
                duplicates.append(name)
        return duplicates

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
        return source_season_episode(source)

    @staticmethod
    def _preferred_media_files(
        files: list[str], context: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        if context.get("mediaType") != "tv":
            return files, []
        strm_files = [name for name in files if name.casefold().endswith(".strm")]
        _, duplicates = select_preferred_episodes(
            strm_files,
            path=lambda name: name,
            default_season=int(context.get("season") or 1),
        )
        duplicate_set = set(duplicates)
        return [name for name in files if name not in duplicate_set], duplicates

    @staticmethod
    def _episode_count(files: list[str], context: dict[str, Any]) -> int:
        strm_files = [name for name in files if name.casefold().endswith(".strm")]
        if context.get("mediaType") != "tv":
            return len(strm_files)
        default_season = int(context.get("season") or 1)
        identities = {
            (season or default_season, episode)
            for name in strm_files
            for season, episode in [source_season_episode(name)]
            if episode is not None
        }
        return len(identities) if identities else len(strm_files)

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
        source_path = str(body.get("source_path") or body.get("sourcePath") or "")
        source_root = str(getattr(self.path_config, "list_root", self.settings.list_root))
        type_dir = type_dir_from_source(source_root, source_path, media_type)
        normalized_category = normalize_category(type_dir, category)
        if normalized_category is None:
            raise AppError(400, "分类不在系统内置媒体目录中")
        category = normalized_category
        folder_name = f"{title} ({year})" if year else title
        season = int(body.get("season") or 1)
        return {
            "targetDir": self._target_directory(
                self.path_config.emby_strm_root,
                media_type,
                category,
                folder_name,
                source_path=source_path,
            ),
            "folderName": folder_name,
            "mediaType": media_type,
            "category": category,
            "title": title,
            "year": year,
            "season": season,
        }

    def _target_directory(
        self,
        root: str,
        media_type: str,
        category: str,
        folder_name: str,
        *,
        source_path: str = "",
    ) -> str:
        type_dir = (
            type_dir_from_source(
                str(getattr(self.path_config, "list_root", self.settings.list_root)),
                source_path,
                media_type,
            )
            if source_path
            else canonical_type_dir("", media_type)
        )
        return media_resource_path(root, type_dir, category, folder_name)

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
        if item.get("mediaType") == "tv":
            synced_files, _ = cls._preferred_media_files(
                synced_files,
                {**item, "season": season},
            )
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
            "episodeCount": cls._episode_count(synced_files, {**item, "season": season}),
            "status": "completed" if item.get("status") == "completed" else "ongoing",
            "totalEpisodes": item.get("totalEpisodes") or "",
            "season": season,
            "seasons": cls._effective_seasons(synced_files, {**item, "season": season}),
            "updateSchedule": item.get("updateSchedule") or "",
            "baiduLink": item.get("baiduLink") or "",
        }
