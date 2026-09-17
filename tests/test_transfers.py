import asyncio
from typing import Any

import pytest

from strmflow.core.config import Settings
from strmflow.services.transfers.base import TransferProvider, TransferResult, TransferSpec
from strmflow.services.transfers.manager import TransferManager


class BlockingProvider(TransferProvider):
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    def command(self, spec: TransferSpec) -> list[str]:
        return ["blocking", spec.destination]

    async def execute(self, spec: TransferSpec) -> TransferResult:
        del spec
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    def capability(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": True}


class RecordingRepository:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update(self, job_id: str, **values: Any) -> None:
        self.updates.append({"jobId": job_id, **values})


async def test_cancelled_transfer_is_persisted_as_failed() -> None:
    provider = BlockingProvider()
    repository = RecordingRepository()
    manager = TransferManager(
        Settings(),
        [provider],
        repository,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(
        manager._run(
            "job-1",
            provider,
            TransferSpec("https://example.test/share", "video/test"),
        )
    )
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(
        update.get("status") == "failed" and "取消" in update.get("stderr", "")
        for update in repository.updates
    )
    assert repository.updates[-1]["jobId"] == "job-1"
    assert repository.updates[-1].get("finished_at") is not None


def test_transfer_command_redaction_hides_share_link_and_extract_code() -> None:
    command = TransferManager._redact(
        [
            "bdpan",
            "transfer",
            "https://pan.baidu.com/s/secret-share?pwd=6666",
            "-p",
            "6666",
        ]
    )

    assert command == [
        "bdpan",
        "transfer",
        "https://pan.baidu.com/s/[已隐藏]",
        "-p",
        "[已隐藏]",
    ]
