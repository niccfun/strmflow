from __future__ import annotations

import asyncio
import shutil

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.services.transfers.base import TransferProvider, TransferResult, TransferSpec


class BdpanTransferProvider(TransferProvider):
    name = "bdpan"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def command(self, spec: TransferSpec) -> list[str]:
        values = {
            "share_url": spec.share_url,
            "destination": spec.destination,
            "extract_code": spec.extract_code,
        }
        try:
            arguments = [
                argument.format_map(values) for argument in self.settings.bdpan_transfer_args
            ]
        except KeyError as exc:
            raise AppError(500, f"BDPAN_TRANSFER_ARGS 含未知占位符：{exc.args[0]}") from exc
        return [self.settings.bdpan_binary, *arguments]

    async def execute(self, spec: TransferSpec) -> TransferResult:
        if not self.settings.bdpan_enabled:
            raise AppError(503, "bdpan 转存接口尚未启用，请设置 BDPAN_ENABLED=true")
        executable = shutil.which(self.settings.bdpan_binary)
        if not executable:
            raise AppError(503, f"未找到 bdpan 二进制：{self.settings.bdpan_binary}")
        argv = self.command(spec)
        argv[0] = executable
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.bdpan_timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AppError(504, "bdpan 转存执行超时") from exc
        return TransferResult(
            return_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace")[-20_000:],
            stderr=stderr.decode(errors="replace")[-20_000:],
        )

    def capability(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.settings.bdpan_enabled,
            "binary": self.settings.bdpan_binary,
            "available": shutil.which(self.settings.bdpan_binary) is not None,
            "placeholders": ["share_url", "destination", "extract_code"],
        }
