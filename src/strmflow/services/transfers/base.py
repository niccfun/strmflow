from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TransferSpec:
    share_url: str
    destination: str
    extract_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransferResult:
    return_code: int
    stdout: str
    stderr: str


class TransferProvider(ABC):
    """转存端口；后续接入其他二进制或 HTTP 服务只需实现该接口。"""

    name: str

    @abstractmethod
    def command(self, spec: TransferSpec) -> list[str]: ...

    @abstractmethod
    async def execute(self, spec: TransferSpec) -> TransferResult: ...

    @abstractmethod
    def capability(self) -> dict[str, Any]: ...
