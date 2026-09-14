from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from strmflow.core.errors import AppError
from strmflow.infrastructure.orm import MediaItemRow


class MediaRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def list(self) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(MediaItemRow).order_by(MediaItemRow.name.collate("NOCASE"))
                )
            ).all()
            return [self._to_dict(row) for row in rows]

    async def get(self, item_id: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(MediaItemRow, item_id)
            return self._to_dict(row) if row else None

    async def get_by_source_path(self, source_path: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(MediaItemRow).where(MediaItemRow.source_path == source_path)
            )
            return self._to_dict(row) if row else None

    async def count(self) -> int:
        async with self.sessions() as session:
            return int(await session.scalar(select(func.count()).select_from(MediaItemRow)) or 0)

    async def upsert(self, item: dict[str, Any]) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            # 与旧 JSON 行为一致：同一个 sourcePath 只能保留一条记录。
            await session.execute(
                delete(MediaItemRow).where(
                    MediaItemRow.source_path == item["sourcePath"], MediaItemRow.id != item["id"]
                )
            )
            row = await session.get(MediaItemRow, item["id"])
            if row is None:
                row = MediaItemRow(id=item["id"])
                session.add(row)
            self._apply(row, item)
            await session.flush()
            return self._to_dict(row)

    async def delete(self, item_id: str) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(MediaItemRow, item_id)
            if row is None:
                raise AppError(404, "媒体配置不存在")
            await session.delete(row)

    async def update(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            row = await session.get(MediaItemRow, item_id)
            if row is None:
                raise AppError(404, "媒体配置不存在")
            merged = {**self._to_dict(row), **patch, "updatedAt": datetime.now(UTC).isoformat()}
            self._apply(row, merged)
            await session.flush()
            return self._to_dict(row)

    async def import_many(self, items: list[dict[str, Any]]) -> int:
        imported = 0
        for item in items:
            if not item.get("id") or not item.get("sourcePath") or not item.get("targetDir"):
                continue
            await self.upsert(item)
            imported += 1
        return imported

    @staticmethod
    def _apply(row: MediaItemRow, item: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        row.source_path = str(item["sourcePath"])
        row.name = str(item.get("name") or item.get("title") or "Media")
        row.generated_path = str(item.get("generatedPath") or item["sourcePath"])
        row.target_dir = str(item["targetDir"])
        row.title = str(item.get("title") or row.name)
        row.year = str(item.get("year") or "")
        row.category = str(item.get("category") or "未分类")
        row.media_type = str(item.get("mediaType") or "tv")
        row.status = "completed" if item.get("status") == "completed" else "ongoing"
        total = item.get("totalEpisodes")
        row.total_episodes = int(total) if str(total or "").isdigit() else None
        season = item.get("season")
        row.season = int(season) if str(season or "").isdigit() else 1
        row.update_schedule = str(item.get("updateSchedule") or "")
        row.baidu_link = str(item.get("baiduLink") or "")
        row.synced_files = list(item.get("syncedFiles") or [])
        row.last_synced_at = MediaRepository._parse_datetime(item.get("lastSyncedAt"))
        row.last_checked_at = MediaRepository._parse_datetime(item.get("lastCheckedAt"))
        row.created_at = MediaRepository._parse_datetime(item.get("createdAt")) or now
        row.updated_at = MediaRepository._parse_datetime(item.get("updatedAt")) or now

    @staticmethod
    def _to_dict(row: MediaItemRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "sourcePath": row.source_path,
            "generatedPath": row.generated_path,
            "targetDir": row.target_dir,
            "title": row.title,
            "year": row.year,
            "category": row.category,
            "mediaType": row.media_type,
            "status": row.status,
            "totalEpisodes": str(row.total_episodes or ""),
            "season": row.season or 1,
            "updateSchedule": row.update_schedule,
            "baiduLink": row.baidu_link,
            "syncedFiles": row.synced_files or [],
            "lastSyncedAt": MediaRepository._iso(row.last_synced_at),
            "lastCheckedAt": MediaRepository._iso(row.last_checked_at),
            "createdAt": MediaRepository._iso(row.created_at),
            "updatedAt": MediaRepository._iso(row.updated_at),
        }

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
