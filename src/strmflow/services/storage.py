from __future__ import annotations

import json
import re
from typing import Any

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.domain.models import Storage, StorageConfig
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.utils.paths import (
    join_virtual_path,
    normalize_virtual_path,
    path_base,
    path_dir,
    relative_virtual_path,
    validate_folder_name,
)

MEDIA_EXTENSION = re.compile(r"\.(strm|mp4|mkv|flv|avi|wmv|ts|rmvb|webm|mov|m4v)$", re.IGNORECASE)
SEASON_FOLDER = re.compile(r"^(season\s*\d+|s\d{1,2}\b|第\s*\d+\s*季)", re.IGNORECASE)


class StorageService:
    def __init__(
        self,
        settings: Settings,
        openlist: OpenListClient,
        path_config: PathConfigService | None = None,
    ) -> None:
        self.settings = settings
        self.openlist = openlist
        self.path_config = path_config

    async def get_config(self) -> StorageConfig:
        data = await self.openlist.request("GET", "/api/admin/storage/list?page=1&per_page=1000")
        raw_content = data.get("content") if isinstance(data, dict) else None
        content = raw_content if isinstance(raw_content, list) else []
        normalized = [self._normalize_storage(item) for item in content]
        local = [item for item in normalized if item.driver == "Local"]
        strm: list[Storage] = []
        for item in normalized:
            if item.driver != "Strm" or item.mount_path == "/":
                continue
            save_path = item.addition.get("SaveStrmLocalPath")
            item.save_to_local = item.addition.get("SaveStrmToLocal") is True and bool(save_path)
            item.source_roots = self._parse_source_roots(item.addition.get("paths", ""))
            item.save_virtual_root = (
                self._local_to_virtual(str(save_path), local)
                if item.save_to_local
                else item.mount_path
            )
            if item.save_to_local and len(item.source_roots) == 1:
                item.auto_flatten_prefix = path_base(item.source_roots[0])
            strm.append(item)
        return StorageConfig(local_storages=local, strm_storages=strm)

    async def list_media_folders(self, refresh: bool = False) -> list[dict[str, Any]]:
        list_root = self.path_config.list_root if self.path_config else self.settings.list_root
        if not list_root:
            raise AppError(400, "请先在设置中指定只读源 STRM 根目录")
        found = await self._list_classified_folders(list_root, refresh)
        return [
            self._enrich_folder(folder, list_root)
            for folder in sorted(found, key=lambda item: item["name"].casefold())
        ]

    async def list_media_options(
        self,
        type_dir: str | None = None,
        category: str | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        list_root = self.path_config.list_root if self.path_config else self.settings.list_root
        if not list_root:
            raise AppError(400, "请先在设置中指定只读源 STRM 根目录")
        root = normalize_virtual_path(list_root)
        selected_type = validate_folder_name(type_dir) if type_dir else ""
        selected_category = validate_folder_name(category) if category else ""
        if selected_category and not selected_type:
            raise AppError(400, "请先选择一级目录")
        parent = join_virtual_path(root, selected_type, selected_category)
        raw_entries = await self.openlist.list_dir(parent, refresh=refresh)
        directories = [
            entry
            for entry in (raw_entries if isinstance(raw_entries, list) else [])
            if entry.get("is_dir") is True and isinstance(entry.get("name"), str)
        ]
        directories.sort(key=lambda entry: entry["name"].casefold())
        if selected_type and selected_category:
            folders = [
                self._enrich_folder(
                    {
                        "name": entry["name"],
                        "modified": entry.get("modified"),
                        "mediaPath": join_virtual_path(parent, entry["name"]),
                        "scanPath": join_virtual_path(parent, entry["name"]),
                        "strmStorage": root,
                        "strmSaveRoot": root,
                    },
                    root,
                )
                for entry in directories
            ]
            return {"level": "resources", "parent": parent, "folders": folders}
        return {
            "level": "categories" if selected_type else "types",
            "parent": parent,
            "entries": [
                {
                    "name": entry["name"],
                    "path": join_virtual_path(parent, entry["name"]),
                    "modified": entry.get("modified"),
                }
                for entry in directories
            ],
        }

    async def _list_classified_folders(self, list_root: str, refresh: bool) -> list[dict[str, Any]]:
        root = normalize_virtual_path(list_root)
        found: list[dict[str, Any]] = []
        type_entries = await self.openlist.list_dir(root, refresh=refresh)
        for type_entry in type_entries if isinstance(type_entries, list) else []:
            if type_entry.get("is_dir") is not True or not isinstance(type_entry.get("name"), str):
                continue
            type_path = join_virtual_path(root, type_entry["name"])
            category_entries = await self.openlist.list_dir(type_path, refresh=refresh)
            for category_entry in category_entries if isinstance(category_entries, list) else []:
                if category_entry.get("is_dir") is not True or not isinstance(
                    category_entry.get("name"), str
                ):
                    continue
                category_path = join_virtual_path(type_path, category_entry["name"])
                resource_entries = await self.openlist.list_dir(category_path, refresh=refresh)
                for resource in resource_entries if isinstance(resource_entries, list) else []:
                    if resource.get("is_dir") is not True or not isinstance(
                        resource.get("name"), str
                    ):
                        continue
                    path = join_virtual_path(category_path, resource["name"])
                    found.append(
                        {
                            "name": resource["name"],
                            "modified": resource.get("modified"),
                            "mediaPath": path,
                            "scanPath": path,
                            "strmStorage": root,
                            "strmSaveRoot": root,
                        }
                    )
                    if len(found) >= 2000:
                        return found
        return found

    @staticmethod
    def _enrich_folder(folder: dict[str, Any], list_root: str = "") -> dict[str, Any]:
        path = normalize_virtual_path(folder.get("scanPath") or folder.get("mediaPath"))
        parts = [part for part in path.split("/") if part]
        lowered = [part.casefold() for part in parts]
        aliases = {
            "tv": "tv",
            "电视剧": "tv",
            "剧集": "tv",
            "shows": "tv",
            "show": "tv",
            "movie": "movie",
            "movies": "movie",
            "电影": "movie",
            "影片": "movie",
        }
        media_type = "tv"
        category = "未分类"
        type_dir = ""
        for index, segment in enumerate(lowered):
            if segment not in aliases:
                continue
            media_type = aliases[segment]
            type_dir = parts[index]
            if index + 2 < len(parts):
                category = parts[index + 1]
            break
        relative = relative_virtual_path(list_root, path) if list_root else None
        relative_parts = [part for part in str(relative or "").split("/") if part]
        if relative_parts:
            type_dir = relative_parts[0]
            if len(relative_parts) >= 3:
                category = relative_parts[1]
            media_type = aliases.get(type_dir.casefold(), media_type)
        name = str(folder.get("name") or path_base(path))
        title_match = re.match(r"^(.*?)\s*[（(](\d{4})[）)]\s*$", name)
        return {
            **folder,
            "sourcePath": path,
            "typeDir": type_dir or (parts[-3] if len(parts) >= 3 else "STRM"),
            "mediaType": media_type,
            "category": category,
            "title": title_match.group(1).strip() if title_match else name,
            "year": title_match.group(2) if title_match else "",
        }

    def resolve_paths(
        self, config: StorageConfig, listed_path: str, list_root: str
    ) -> dict[str, str]:
        listed = normalize_virtual_path(listed_path)
        root = normalize_virtual_path(list_root)
        fallbacks: list[Storage] = []
        for storage in config.strm_storages:
            relative_mount = relative_virtual_path(storage.mount_path, listed)
            if relative_mount is not None:
                return {
                    "mediaPath": join_virtual_path(
                        storage.save_virtual_root,
                        self._add_flatten(storage, relative_mount),
                    )
                    if storage.save_to_local
                    else listed,
                    "scanPath": listed,
                    "strmStorage": storage.mount_path,
                    "strmSaveRoot": storage.save_virtual_root,
                }
            if not storage.save_to_local:
                continue
            relative_saved = relative_virtual_path(storage.save_virtual_root, listed)
            if relative_saved is None:
                fallbacks.append(storage)
                continue
            return {
                "mediaPath": listed,
                "scanPath": join_virtual_path(
                    storage.mount_path, self._strip_flatten(storage, relative_saved)
                ),
                "strmStorage": storage.mount_path,
                "strmSaveRoot": storage.save_virtual_root,
            }
        for storage in fallbacks:
            relative = (
                relative_virtual_path(path_dir(root), listed)
                or relative_virtual_path(root, listed)
                or path_base(listed)
            )
            return {
                "mediaPath": join_virtual_path(
                    storage.save_virtual_root, self._add_flatten(storage, relative)
                ),
                "scanPath": join_virtual_path(storage.mount_path, relative),
                "strmStorage": storage.mount_path,
                "strmSaveRoot": storage.save_virtual_root,
            }
        raise AppError(500, f"无法根据 Strm 存储配置推导路径：{listed_path}")

    async def resolve_underlying_source_path(self, listed_path: str) -> str | None:
        """Map a virtual Strm path back to its single configured source tree.

        A generated ``.strm`` entry and a real ``.strm`` file have the same
        appearance through a Strm mount.  Callers that validate the source
        inventory must therefore inspect the underlying storage instead of the
        generated view.
        """
        listed = normalize_virtual_path(listed_path)
        config = await self.get_config()
        for storage in config.strm_storages:
            relative = relative_virtual_path(storage.mount_path, listed)
            if relative is None or len(storage.source_roots) != 1:
                continue
            return join_virtual_path(storage.source_roots[0], relative)
        return None

    async def assert_publish_target_isolated(self, listed_source: str, target: str) -> None:
        """Reject a STRM output tree that overlaps its virtual or physical input."""
        source = normalize_virtual_path(listed_source)
        destination = normalize_virtual_path(target)
        if self._paths_overlap(source, destination):
            raise AppError(409, "目标 STRM 目录不能与只读源 STRM 目录重叠")
        underlying = await self.resolve_underlying_source_path(source)
        if underlying and self._paths_overlap(underlying, destination):
            raise AppError(
                409,
                f"目标 STRM 目录与网盘原始媒体目录重叠：{underlying}",
            )

    @staticmethod
    def _paths_overlap(left: str, right: str) -> bool:
        return (
            relative_virtual_path(left, right) is not None
            or relative_virtual_path(right, left) is not None
        )

    async def _list_children(
        self, config: StorageConfig | None, root: str, refresh: bool
    ) -> list[dict[str, Any]]:
        root = normalize_virtual_path(root)
        raw_entries = await self.openlist.list_dir(root, refresh=refresh)
        entries = raw_entries if isinstance(raw_entries, list) else []
        result = []
        for entry in entries:
            if entry.get("is_dir") is not True or not isinstance(entry.get("name"), str):
                continue
            path = join_virtual_path(root, entry["name"])
            resolved = (
                self.resolve_paths(config, path, root)
                if config is not None
                else {
                    "mediaPath": path,
                    "scanPath": path,
                    "strmStorage": root,
                    "strmSaveRoot": root,
                }
            )
            result.append(
                {
                    "name": entry["name"],
                    "modified": entry.get("modified"),
                    **resolved,
                }
            )
        return sorted(result, key=lambda item: item["name"].casefold())

    async def _discover(
        self,
        config: StorageConfig | None,
        root: str,
        path: str,
        refresh: bool,
        depth: int,
        found: list[dict[str, Any]],
        seen: set[str],
    ) -> None:
        if depth < 0 or path in seen or len(found) >= 2000:
            return
        seen.add(path)
        raw_entries = await self.openlist.list_dir(path, refresh=refresh)
        entries = raw_entries if isinstance(raw_entries, list) else []
        if any(
            (entry.get("is_dir") is True and SEASON_FOLDER.search(str(entry.get("name", ""))))
            or (
                entry.get("is_dir") is not True
                and MEDIA_EXTENSION.search(str(entry.get("name", "")))
            )
            for entry in entries
        ):
            modified = sorted(filter(None, (entry.get("modified") for entry in entries)))
            found.append(
                {
                    "name": path_base(path),
                    "modified": modified[-1] if modified else None,
                    **(
                        self.resolve_paths(config, path, root)
                        if config is not None
                        else {
                            "mediaPath": path,
                            "scanPath": path,
                            "strmStorage": root,
                            "strmSaveRoot": root,
                        }
                    ),
                }
            )
            return
        for entry in entries:
            if entry.get("is_dir") is True and isinstance(entry.get("name"), str):
                await self._discover(
                    config,
                    root,
                    join_virtual_path(path, entry["name"]),
                    refresh,
                    depth - 1,
                    found,
                    seen,
                )

    @staticmethod
    def _normalize_storage(value: dict[str, Any]) -> Storage:
        addition = value.get("addition") or {}
        if isinstance(addition, str):
            try:
                addition = json.loads(addition)
            except ValueError:
                addition = {}
        return Storage(
            id=value.get("id"),
            driver=str(value.get("driver", "")),
            mount_path=normalize_virtual_path(value.get("mount_path") or value.get("mountPath")),
            addition=addition if isinstance(addition, dict) else {},
        )

    @staticmethod
    def _parse_source_roots(value: object) -> list[str]:
        result: list[str] = []
        for raw in str(value or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            separator = line.find(":")
            if separator > 0 and "/" not in line[:separator]:
                line = line[separator + 1 :]
            path = normalize_virtual_path(line)
            if path not in result:
                result.append(path)
        return result

    @staticmethod
    def _local_to_virtual(path: str, local: list[Storage]) -> str:
        normalized = normalize_virtual_path(path)
        candidates: list[tuple[int, Storage, str]] = []
        for storage in local:
            disk_root = normalize_virtual_path(storage.addition.get("root_folder_path", ""))
            relative = relative_virtual_path(disk_root, normalized)
            if disk_root != "/" and relative is not None:
                candidates.append((len(disk_root), storage, relative))
        if not candidates:
            return normalized
        _, storage, relative = max(candidates, key=lambda item: item[0])
        return join_virtual_path(storage.mount_path, relative)

    @staticmethod
    def _add_flatten(storage: Storage, relative: str) -> str:
        relative = relative.lstrip("/")
        prefix = storage.auto_flatten_prefix
        if not prefix or not relative or relative == prefix or relative.startswith(f"{prefix}/"):
            return relative
        return f"{prefix}/{relative}"

    @staticmethod
    def _strip_flatten(storage: Storage, relative: str) -> str:
        relative = relative.lstrip("/")
        prefix = storage.auto_flatten_prefix
        if relative == prefix:
            return ""
        if prefix and relative.startswith(f"{prefix}/"):
            return relative[len(prefix) + 1 :]
        return relative
