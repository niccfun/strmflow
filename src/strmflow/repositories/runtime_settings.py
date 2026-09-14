from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from strmflow.infrastructure.orm import AppMetadataRow

PATH_CONFIG_KEY = "runtime_path_config_v1"
EMBY302_CONFIG_KEY = "runtime_emby302_config_v1"


class RuntimeSettingsRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def load_paths(self) -> dict[str, Any] | None:
        return await self._load(PATH_CONFIG_KEY)

    async def save_paths(self, value: dict[str, str]) -> None:
        await self._save(PATH_CONFIG_KEY, value)

    async def load_emby302(self) -> dict[str, Any] | None:
        return await self._load(EMBY302_CONFIG_KEY)

    async def save_emby302(self, value: dict[str, Any]) -> None:
        await self._save(EMBY302_CONFIG_KEY, value)

    async def _load(self, key: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(AppMetadataRow, key)
            if row is None:
                return None
            try:
                value = json.loads(row.value)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None

    async def _save(self, key: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        async with self.sessions.begin() as session:
            row = await session.get(AppMetadataRow, key)
            if row is None:
                session.add(AppMetadataRow(key=key, value=encoded))
            else:
                row.value = encoded
