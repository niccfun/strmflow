from __future__ import annotations

from typing import Any

import pytest

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.services.bdpan import BdpanCli, BdpanRunResult
from strmflow.services.bdpan_automation import BdpanAutomationService, ShareMediaFile


class FakeRuntimeRepository:
    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None
        self.states: dict[str, Any] | None = None

    async def load_bdpan(self) -> dict[str, Any] | None:
        return self.config

    async def save_bdpan(self, value: dict[str, Any]) -> None:
        self.config = value

    async def load_bdpan_watch_states(self) -> dict[str, Any] | None:
        return self.states

    async def save_bdpan_watch_states(self, value: dict[str, Any]) -> None:
        self.states = value


class FakeMedia:
    def __init__(self) -> None:
        self.published: list[str] = []
        self.item = {
            "id": "m1",
            "name": "交锋 (2026)",
            "mediaType": "tv",
            "category": "国产剧",
            "status": "ongoing",
            "sourcePath": "/temp_strm/TV/国产剧/交锋 (2026)",
            "baiduLink": "https://pan.baidu.com/s/example?pwd=ab12",
        }

    async def list_items(self) -> list[dict[str, Any]]:
        return [dict(self.item)]

    async def get_item(self, item_id: str) -> dict[str, Any]:
        assert item_id == self.item["id"]
        return dict(self.item)

    async def publish(self, request: Any) -> dict[str, Any]:
        self.published.append(request.id)
        return {"copied": 1, "newFiles": ["Season 02/交锋.S02E01.strm"]}


class FakeOpenList:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.requests.append((method, path, body))
        return {"is_done": True} if path.endswith("/progress") else {}


class FakeEmby:
    def __init__(self) -> None:
        self.refreshes = 0

    async def refresh_library(self) -> None:
        self.refreshes += 1


class FakePathConfig:
    list_root = "/temp_strm"


class FakeBdpanCli(BdpanCli):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.executed: list[list[str]] = []

    async def status(self, binary: str | None = None) -> dict[str, Any]:
        return {
            "available": True,
            "loggedIn": True,
            "version": "3.8.7",
            "binary": binary or "bdpan",
        }

    async def execute(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        require_json: bool = False,
    ) -> BdpanRunResult:
        del timeout, stdin, require_json
        self.executed.append(argv)
        payload = {"status": "submitted", "task_id": f"task-{len(self.executed)}"}
        return BdpanRunResult(0, '{"status":"submitted"}', "", payload)


def media_file(fsid: str, path: str, *, size: int = 100) -> ShareMediaFile:
    parts = tuple(path.split("/"))
    return ShareMediaFile(
        fsid=fsid,
        name=parts[-1],
        relative_parts=parts,
        size=size,
        modified="2026-09-14T12:00:00+08:00",
    )


def test_share_input_and_official_commands_are_normalized() -> None:
    cli = BdpanCli(Settings())
    url, code = cli.parse_share_input(
        "分享：https://pan.baidu.com/s/demo?pwd=a1B2&from=copy 提取码：zzzz"
    )
    assert url == "https://pan.baidu.com/s/demo?from=copy"
    assert code == "a1B2"

    command = cli.select_command(
        url,
        ["9007199254740993123"],
        "/apps/bdpan/StrmFlow/TV/交锋 (2026)/Season 02",
        code,
        session_id="1784035443-a1b2c3",
    )
    assert command[:3] == ["bdpan", "transfer", "select"]
    assert command[command.index("--fsid") + 1] == "9007199254740993123"
    assert command[command.index("-d") + 1] == "StrmFlow/TV/交锋 (2026)/Season 02"
    assert command[command.index("--session-id") + 1] == "1784035443-a1b2c3"
    assert "--json" in command
    assert "--no-check-update" in command


@pytest.mark.asyncio
async def test_automation_establishes_baseline_then_selects_only_new_files() -> None:
    settings = Settings(bdpan_binary="bdpan", bdpan_save_root="StrmFlow")
    cli = FakeBdpanCli(settings)
    repository = FakeRuntimeRepository()
    media = FakeMedia()
    service = BdpanAutomationService(
        settings,
        cli,
        repository,  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    existing = media_file("1001", "Season 01/交锋.S01E01.mp4")
    addition = media_file("1002", "Season 02/交锋.S02E01.mp4")
    rounds = [[existing], [existing, addition]]

    async def list_share_media(
        _share_url: str, _extract_code: str, _session_id: str
    ) -> list[ShareMediaFile]:
        return rounds.pop(0)

    service._list_share_media = list_share_media  # type: ignore[method-assign]

    baseline = await service.check_item("m1")
    assert baseline == {
        "itemId": "m1",
        "baseline": True,
        "fileCount": 1,
        "newCount": 0,
    }
    assert cli.executed == []

    result = await service.check_item("m1")
    assert result["newCount"] == 1
    assert result["submittedCount"] == 1
    assert len(cli.executed) == 1
    command = cli.executed[0]
    assert command[command.index("--fsid") + 1] == "1002"
    assert command[command.index("-d") + 1] == ("StrmFlow/TV/国产剧/交锋 (2026)/Season 02")
    assert service.states["m1"]["pendingSyncAt"]
    assert service.states["m1"]["submittedTasks"][0]["taskId"] == "task-1"
    assert repository.states == service.states


def test_share_page_accepts_documented_payload_shape() -> None:
    items, has_more = BdpanAutomationService._share_page(
        {"errno": 0, "data": {"count": 1, "has_more": True, "list": [{"fs_id": "1"}]}}
    )
    assert items == [{"fs_id": "1"}]
    assert has_more is True


def test_single_season_wrapper_is_preserved() -> None:
    file = media_file("1002", "Season 02/交锋.E01.mp4")
    assert BdpanAutomationService._strip_single_wrapper([file])[0].relative_parts == (
        "Season 02",
        "交锋.E01.mp4",
    )

    show_wrapped = media_file("1003", "交锋 (2026)/Season 02/交锋.E01.mp4")
    assert BdpanAutomationService._strip_single_wrapper([show_wrapped])[0].relative_parts == (
        "Season 02",
        "交锋.E01.mp4",
    )


@pytest.mark.asyncio
async def test_pending_transfer_triggers_openlist_publish_and_emby_refresh() -> None:
    settings = Settings(
        bdpan_binary="bdpan",
        emby_url="http://emby:8096",
        emby_api_key="key",
    )
    media = FakeMedia()
    openlist = FakeOpenList()
    emby = FakeEmby()
    repository = FakeRuntimeRepository()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        repository,  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        openlist,  # type: ignore[arg-type]
        emby,  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    service.states["m1"] = {"pendingSyncAttempts": 0, "pendingSyncAt": "now"}

    await service._sync_item("m1")

    assert openlist.requests[0] == (
        "POST",
        "/api/admin/scan/start",
        {"path": media.item["sourcePath"], "limit": settings.scan_limit},
    )
    assert openlist.requests[1][:2] == ("GET", "/api/admin/scan/progress")
    assert media.published == ["m1"]
    assert emby.refreshes == 1
    assert service.states["m1"]["pendingSyncAt"] is None
    assert service.states["m1"]["lastSyncedAt"]
