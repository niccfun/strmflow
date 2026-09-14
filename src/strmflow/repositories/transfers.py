from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from strmflow.core.errors import AppError
from strmflow.infrastructure.orm import TransferJobRow
from strmflow.schemas.api import TransferJob


class TransferJobRepository:
    def __init__(self, sessions: async_sessionmaker, retention: int) -> None:
        self.sessions = sessions
        self.retention = retention

    async def create(self, job: TransferJob, metadata: dict[str, Any]) -> TransferJob:
        async with self.sessions.begin() as session:
            session.add(
                TransferJobRow(
                    id=job.id,
                    provider=job.provider,
                    status=job.status,
                    destination=job.destination,
                    command=job.command,
                    job_metadata=metadata,
                    created_at=job.created_at,
                    stdout="",
                    stderr="",
                )
            )
        await self._prune()
        return job

    async def get(self, job_id: str) -> TransferJob:
        async with self.sessions() as session:
            row = await session.get(TransferJobRow, job_id)
            if row is None:
                raise AppError(404, "转存任务不存在")
            return self._to_model(row)

    async def list(self) -> list[TransferJob]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(TransferJobRow)
                    .order_by(TransferJobRow.created_at.desc())
                    .limit(self.retention)
                )
            ).all()
            return [self._to_model(row) for row in rows]

    async def update(self, job_id: str, **values: Any) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(TransferJobRow).where(TransferJobRow.id == job_id).values(**values)
            )

    async def fail_interrupted(self) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(TransferJobRow)
                .where(TransferJobRow.status.in_(["queued", "running"]))
                .values(
                    status="failed",
                    finished_at=datetime.now(UTC),
                    stderr="服务重启，任务执行被中断",
                )
            )

    async def _prune(self) -> None:
        # SQLite 兼容的简单保留策略；历史任务超过上限时删除最旧记录。
        async with self.sessions.begin() as session:
            old_ids = (
                await session.scalars(
                    select(TransferJobRow.id)
                    .order_by(TransferJobRow.created_at.desc())
                    .offset(self.retention)
                )
            ).all()
            if old_ids:
                for row in await session.scalars(
                    select(TransferJobRow).where(TransferJobRow.id.in_(old_ids))
                ):
                    await session.delete(row)

    @staticmethod
    def _to_model(row: TransferJobRow) -> TransferJob:
        def aware(value: datetime | None) -> datetime | None:
            return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value

        return TransferJob(
            id=row.id,
            provider=row.provider,
            status=row.status,
            destination=row.destination,
            command=row.command,
            created_at=aware(row.created_at),
            started_at=aware(row.started_at),
            finished_at=aware(row.finished_at),
            return_code=row.return_code,
            stdout=row.stdout,
            stderr=row.stderr,
        )
