from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from strmflow.infrastructure.orm import MediaProbeRow


class MediaProbeRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions

    async def list_all(self) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (await session.scalars(select(MediaProbeRow))).all()
            return [self._to_dict(row) for row in rows]

    async def status_counts(self) -> dict[str, int]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(MediaProbeRow.status, func.count()).group_by(MediaProbeRow.status)
                )
            ).all()
        return {str(status): int(count) for status, count in rows}

    async def enqueue(self, target_path: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            row = await session.get(MediaProbeRow, target_path)
            if row is None:
                row = MediaProbeRow(
                    target_path=target_path,
                    item_id="",
                    status="queued",
                    attempts=0,
                    last_error="",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.status = "queued"
                row.attempts = 0
                row.next_attempt_at = None
                row.last_error = ""
                row.updated_at = now
            await session.flush()
            return self._to_dict(row)

    async def mark_running(self, target_path: str) -> int:
        async with self.sessions.begin() as session:
            row = await session.get(MediaProbeRow, target_path)
            if row is None:
                return 0
            row.status = "running"
            row.attempts += 1
            row.next_attempt_at = None
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return row.attempts

    async def complete(
        self,
        target_path: str,
        *,
        item_id: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            row = await session.get(MediaProbeRow, target_path)
            if row is None:
                return None
            row.item_id = item_id
            row.status = "complete"
            row.last_error = ""
            row.next_attempt_at = None
            row.probed_at = now
            row.updated_at = now
            await session.flush()
            return self._to_dict(row)

    async def retry(self, target_path: str, error: str, next_attempt_at: datetime | None) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(MediaProbeRow, target_path)
            if row is None:
                return
            row.status = "retry" if next_attempt_at else "failed"
            row.last_error = error[:1000]
            row.next_attempt_at = next_attempt_at
            row.updated_at = datetime.now(UTC)

    @staticmethod
    def _to_dict(row: MediaProbeRow) -> dict[str, Any]:
        return {
            "targetPath": row.target_path,
            "itemId": row.item_id,
            "status": row.status,
            "attempts": row.attempts,
            "lastError": row.last_error,
            "nextAttemptAt": MediaProbeRepository._iso(row.next_attempt_at),
            "probedAt": MediaProbeRepository._iso(row.probed_at),
        }

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
