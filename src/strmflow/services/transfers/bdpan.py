from __future__ import annotations

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.services.bdpan import BdpanCli, BdpanCliError
from strmflow.services.transfers.base import TransferProvider, TransferResult, TransferSpec


class BdpanTransferProvider(TransferProvider):
    name = "bdpan"

    def __init__(self, settings: Settings, cli: BdpanCli | None = None) -> None:
        self.settings = settings
        self.cli = cli or BdpanCli(settings)

    def command(self, spec: TransferSpec) -> list[str]:
        return self.cli.transfer_command(
            spec.share_url,
            spec.destination,
            spec.extract_code,
            binary=self.settings.bdpan_binary,
        )

    async def execute(self, spec: TransferSpec) -> TransferResult:
        if not self.settings.bdpan_enabled:
            raise AppError(503, "bdpan 转存接口尚未启用，请设置 BDPAN_ENABLED=true")
        argv = self.command(spec)
        try:
            result = await self.cli.execute(
                argv,
                timeout=self.settings.bdpan_timeout,
                require_json=True,
            )
        except BdpanCliError as exc:
            raise AppError(502, str(exc)) from exc
        return TransferResult(
            return_code=result.return_code,
            stdout=result.stdout[-20_000:],
            stderr=result.stderr[-20_000:],
        )

    def capability(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.settings.bdpan_enabled,
            "binary": self.settings.bdpan_binary,
            "available": self.cli.executable(self.settings.bdpan_binary) is not None,
            "officialCli": True,
            "commands": ["transfer", "transfer list", "transfer select"],
        }
