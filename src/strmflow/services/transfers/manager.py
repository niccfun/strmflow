from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Any

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.repositories.transfers import TransferJobRepository
from strmflow.schemas.api import TransferCreateRequest, TransferJob
from strmflow.services.transfers.base import TransferProvider, TransferSpec


class TransferManager:
    def __init__(
        self,
        settings: Settings,
        providers: list[TransferProvider],
        repository: TransferJobRepository,
    ) -> None:
        self.settings = settings
        self.providers = {provider.name: provider for provider in providers}
        self.repository = repository
        self.tasks: set[asyncio.Task[None]] = set()

    def capabilities(self) -> list[dict[str, Any]]:
        return [provider.capability() for provider in self.providers.values()]

    def preview(self, request: TransferCreateRequest) -> dict[str, Any]:
        provider = self._provider(request.provider)
        spec = self._spec(request)
        return {"provider": provider.name, "command": provider.command(spec)}

    async def enqueue(self, request: TransferCreateRequest) -> TransferJob:
        provider = self._provider(request.provider)
        spec = self._spec(request)
        if not provider.capability().get("enabled"):
            raise AppError(503, f"{provider.name} 转存接口尚未启用")
        job = TransferJob(
            id=secrets.token_hex(8),
            provider=provider.name,
            status="queued",
            destination=spec.destination,
            command=provider.command(spec),
            created_at=datetime.now(UTC),
        )
        await self.repository.create(job, spec.metadata)
        task = asyncio.create_task(self._run(job.id, provider, spec))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def get(self, job_id: str) -> TransferJob:
        return await self.repository.get(job_id)

    async def list(self) -> list[TransferJob]:
        return await self.repository.list()

    async def initialize(self) -> None:
        await self.repository.fail_interrupted()

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run(self, job_id: str, provider: TransferProvider, spec: TransferSpec) -> None:
        await self.repository.update(job_id, status="running", started_at=datetime.now(UTC))
        try:
            result = await provider.execute(spec)
            await self.repository.update(
                job_id,
                return_code=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
                status="succeeded" if result.return_code == 0 else "failed",
            )
        except Exception as exc:  # noqa: BLE001 - persist every background failure on the job
            await self.repository.update(job_id, status="failed", stderr=str(exc))
        finally:
            await self.repository.update(job_id, finished_at=datetime.now(UTC))

    def _provider(self, name: str) -> TransferProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise AppError(400, f"未知转存提供方：{name}") from exc

    @staticmethod
    def _spec(request: TransferCreateRequest) -> TransferSpec:
        if not request.share_url.strip():
            raise AppError(400, "缺少分享链接")
        if not request.destination.strip():
            raise AppError(400, "缺少转存目标目录")
        return TransferSpec(
            share_url=request.share_url.strip(),
            destination=request.destination.strip(),
            extract_code=request.extract_code.strip(),
            metadata=request.metadata,
        )
