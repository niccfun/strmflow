from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.infrastructure.orm import AppMetadataRow
from strmflow.repositories.media import MediaRepository
from strmflow.services.openlist import OpenListClient

logger = logging.getLogger(__name__)
MIGRATION_KEY = "legacy_openlist_json_import_v1"


class LegacyJsonImporter:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        media: MediaRepository,
        openlist: OpenListClient,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.media = media
        self.openlist = openlist

    async def run_once(self) -> int:
        if not self.settings.legacy_json_import or await self._completed():
            return 0
        if await self.media.count() > 0:
            await self._mark("skipped: database already contains media items")
            return 0
        try:
            text = await self.openlist.read_text(self.settings.resolved_media_db_path)
        except AppError as exc:
            if exc.status_code == 404:
                await self._mark("completed: legacy file not found")
                return 0
            logger.warning("旧 JSON 导入暂缓：%s", exc.message)
            return 0
        try:
            payload = json.loads(text)
        except ValueError:
            logger.warning(
                "旧 JSON 不是有效 JSON，未执行导入：%s", self.settings.resolved_media_db_path
            )
            return 0
        items = payload.get("items", []) if isinstance(payload, dict) else []
        count = await self.media.import_many(items if isinstance(items, list) else [])
        await self._mark(f"completed: imported {count} item(s)")
        logger.info("已从 OpenList JSON 导入 %s 条媒体记录到 SQLite", count)
        return count

    async def _completed(self) -> bool:
        async with self.sessions() as session:
            return await session.get(AppMetadataRow, MIGRATION_KEY) is not None

    async def _mark(self, value: str) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(AppMetadataRow, MIGRATION_KEY)
            if row is None:
                session.add(AppMetadataRow(key=MIGRATION_KEY, value=value))
            else:
                row.value = value
