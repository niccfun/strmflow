from __future__ import annotations

import asyncio
from typing import Any

from strmflow.core.config import Settings
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.utils.paths import normalize_virtual_path, validate_virtual_path


class PathConfigService:
    def __init__(self, settings: Settings, repository: RuntimeSettingsRepository) -> None:
        self.settings = settings
        self.repository = repository
        self.list_root = (
            normalize_virtual_path(settings.list_root) if settings.list_root.strip() else ""
        )
        self.emby_strm_root = normalize_virtual_path(settings.emby_strm_root)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        stored = await self.repository.load_paths()
        if stored:
            self._apply(stored)

    async def update(self, value: dict[str, Any]) -> dict[str, str]:
        list_root_value = str(value.get("listRoot") or "").strip()
        config = {
            "listRoot": validate_virtual_path(list_root_value, "只读源 STRM 根目录"),
            "embyStrmRoot": validate_virtual_path(value.get("embyStrmRoot"), "目标 STRM 根目录"),
        }
        async with self._lock:
            await self.repository.save_paths(config)
            self._apply(config)
        return self.as_dict()

    def as_dict(self) -> dict[str, str]:
        return {"listRoot": self.list_root, "embyStrmRoot": self.emby_strm_root}

    def _apply(self, value: dict[str, Any]) -> None:
        raw_list_root = str(value.get("listRoot") or "").strip()
        raw_emby_root = str(value.get("embyStrmRoot") or self.settings.emby_strm_root).strip()
        self.list_root = normalize_virtual_path(raw_list_root) if raw_list_root else ""
        self.emby_strm_root = normalize_virtual_path(raw_emby_root)
