from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.repositories.transfers import TransferJobRepository
from strmflow.schemas.api import TransferCreateRequest, TransferJob
from strmflow.services.transfers.base import TransferProvider, TransferSpec


class TransferManager:
    def __init__(
        self,
        settings: Settings,
        providers: list[TransferProvider],
        repository: TransferJobRepository,
        runtime_logs: RuntimeLogStore | None = None,
    ) -> None:
        self.settings = settings
        self.providers = {provider.name: provider for provider in providers}
        self.repository = repository
        self.runtime_logs = runtime_logs
        self.tasks: set[asyncio.Task[None]] = set()

    def capabilities(self) -> list[dict[str, Any]]:
        return [provider.capability() for provider in self.providers.values()]

    def preview(self, request: TransferCreateRequest) -> dict[str, Any]:
        provider = self._provider(request.provider)
        spec = self._spec(request)
        return {"provider": provider.name, "command": self._redact(provider.command(spec))}

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
            command=self._redact(provider.command(spec)),
            created_at=datetime.now(UTC),
        )
        await self.repository.create(job, spec.metadata)
        self._log(
            "info",
            f"转存任务已加入队列：{provider.name}",
            jobId=job.id,
            destination=job.destination,
        )
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
        self._log(
            "success",
            "转存任务管理器初始化完成",
            providers=list(self.providers),
        )

    async def close(self) -> None:
        if self.tasks:
            self._log("info", "正在停止转存任务", activeTaskCount=len(self.tasks))
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run(self, job_id: str, provider: TransferProvider, spec: TransferSpec) -> None:
        await self.repository.update(job_id, status="running", started_at=datetime.now(UTC))
        self._log(
            "info",
            f"转存任务开始执行：{provider.name}",
            jobId=job_id,
            destination=spec.destination,
        )
        try:
            result = await provider.execute(spec)
            await self.repository.update(
                job_id,
                return_code=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
                status="succeeded" if result.return_code == 0 else "failed",
            )
            self._log(
                "success" if result.return_code == 0 else "error",
                f"转存任务{'完成' if result.return_code == 0 else '失败'}：{provider.name}",
                jobId=job_id,
                returnCode=result.return_code,
                destination=spec.destination,
            )
        except asyncio.CancelledError:
            await self.repository.update(
                job_id,
                status="failed",
                stderr="服务停止，任务执行被取消",
            )
            self._log(
                "warning",
                f"转存任务已取消：{provider.name}",
                jobId=job_id,
                destination=spec.destination,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - persist every background failure on the job
            await self.repository.update(job_id, status="failed", stderr=str(exc))
            self._log(
                "error",
                f"转存任务异常：{str(exc)[:300]}",
                jobId=job_id,
                provider=provider.name,
            )
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

    @staticmethod
    def _redact(command: list[str]) -> list[str]:
        result = list(command)
        for index, value in enumerate(result):
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            if parsed.scheme in {"http", "https"} and parsed.hostname == "pan.baidu.com":
                result[index] = "https://pan.baidu.com/s/[已隐藏]"
        for index, value in enumerate(result[:-1]):
            if value in {"-p", "--pwd", "--extract-code"}:
                result[index + 1] = "[已隐藏]"
            if value == "--session-id":
                result[index + 1] = "[会话]"
        return result

    def _log(self, level: str, message: str, **details: Any) -> None:
        if self.runtime_logs:
            self.runtime_logs.add(category="transfer", level=level, message=message, **details)
