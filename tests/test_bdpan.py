from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.schemas.api import BdpanShareImportRequest
from strmflow.services.bdpan import BdpanCli, BdpanCliError, BdpanRunResult
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

    async def collect_files(self, root: str, *, tolerant: bool = False) -> list[str]:
        assert root == self.item["sourcePath"]
        assert tolerant is False
        return ["Season 01/交锋.S01E01.strm"]


class FakeAlreadySyncedMedia(FakeMedia):
    async def publish(self, request: Any) -> dict[str, Any]:
        self.published.append(request.id)
        return {
            "copied": 0,
            "newFiles": [],
            "episodeCount": 3,
            "totalFiles": 3,
        }


class FakeImportMedia:
    def __init__(self) -> None:
        self.saved: Any = None

    async def list_items(self) -> list[dict[str, Any]]:
        return []

    async def save_item(self, body: Any) -> dict[str, Any]:
        self.saved = body
        return {
            "id": "m2",
            "name": f"{body.title} ({body.year})" if body.year else body.title,
            "title": body.title,
            "year": str(body.year or ""),
            "mediaType": body.media_type,
            "category": body.category,
            "status": body.status,
            "sourcePath": body.source_path,
            "baiduLink": body.baidu_link,
        }


class FakeOpenList:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.requests.append((method, path, body))
        return {"is_done": True} if path.endswith("/progress") else {}

    async def remove(self, directory: str, names: list[str]) -> None:
        self.requests.append(("REMOVE", directory, {"names": names}))


class FakeEmby:
    def __init__(self) -> None:
        self.refreshes = 0

    async def refresh_library(self) -> None:
        self.refreshes += 1


class FakeNotifications:
    def __init__(self) -> None:
        self.episode_updates: list[tuple[str, int, int, list[str]]] = []
        self.invalid_links: list[tuple[str, str]] = []

    async def notify_episode_update(
        self,
        item: dict[str, Any],
        new_count: int,
        current_count: int,
        episodes: list[str] | None = None,
    ) -> bool:
        self.episode_updates.append((item["id"], new_count, current_count, episodes or []))
        return True

    async def notify_link_invalid(self, item: dict[str, Any], error: Exception) -> bool:
        self.invalid_links.append((item["id"], str(error)))
        return True


class FakePathConfig:
    list_root = "/temp_strm"


class FakeSourceStorage:
    async def resolve_underlying_source_path(self, path: str) -> str | None:
        assert path == "/temp_strm/TV/国产剧/交锋 (2026)"
        return "/bdpan/apps/bdpan/media/TV/国产剧/交锋 (2026)"


class FakeUnderlyingMedia(FakeMedia):
    def __init__(self, underlying_files: list[str]) -> None:
        super().__init__()
        self.item["sourcePath"] = "/temp_strm/TV/国产剧/交锋 (2026)"
        self.underlying_files = underlying_files

    async def collect_files(self, root: str, *, tolerant: bool = False) -> list[str]:
        assert tolerant is False
        if root.startswith("/bdpan/"):
            return list(self.underlying_files)
        return ["交锋.S01E01.strm", "交锋.S01E02.strm"]


class FakeBdpanCli(BdpanCli):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.executed: list[list[str]] = []
        self.logged_in = True
        self.logout_calls = 0

    async def status(self, binary: str | None = None) -> dict[str, Any]:
        return {
            "available": True,
            "loggedIn": self.logged_in,
            "username": "测试账号" if self.logged_in else "",
            "version": "3.8.7",
            "binary": binary or "bdpan",
        }

    async def logout(self, binary: str | None = None) -> None:
        assert binary in {None, "bdpan"}
        self.logout_calls += 1
        self.logged_in = False

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
async def test_cli_status_reads_account_name_from_whoami_json(monkeypatch) -> None:
    cli = BdpanCli(Settings())
    monkeypatch.setattr(cli, "executable", lambda _binary: "/usr/local/bin/bdpan")
    commands: list[list[str]] = []

    async def execute(
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        require_json: bool = False,
    ) -> BdpanRunResult:
        del timeout, stdin
        commands.append(argv)
        if "version" in argv:
            return BdpanRunResult(0, "bdpan: 3.8.7", "")
        assert require_json is True
        return BdpanRunResult(
            0,
            '{"authenticated":true,"username":"测试账号","has_valid_token":true,"expires_at":"2026-10-01T08:00:00+08:00"}',
            "",
            {
                "authenticated": True,
                "username": "测试账号",
                "has_valid_token": True,
                "expires_at": "2026-10-01T08:00:00+08:00",
            },
        )

    monkeypatch.setattr(cli, "execute", execute)

    status = await cli.status("bdpan")

    assert status["loggedIn"] is True
    assert status["username"] == "测试账号"
    assert status["expiresAt"] == "2026-10-01T08:00:00+08:00"
    whoami = next(command for command in commands if "whoami" in command)
    assert "--json" in whoami


@pytest.mark.asyncio
async def test_cli_status_omits_account_name_when_token_is_invalid(monkeypatch) -> None:
    cli = BdpanCli(Settings())
    monkeypatch.setattr(cli, "executable", lambda _binary: "/usr/local/bin/bdpan")

    async def execute(
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        require_json: bool = False,
    ) -> BdpanRunResult:
        del timeout, stdin, require_json
        if "version" in argv:
            return BdpanRunResult(0, "bdpan: 3.8.7", "")
        return BdpanRunResult(
            0,
            '{"authenticated":true,"username":"过期账号","has_valid_token":false}',
            "",
            {
                "authenticated": True,
                "username": "过期账号",
                "has_valid_token": False,
            },
        )

    monkeypatch.setattr(cli, "execute", execute)

    status = await cli.status("bdpan")

    assert status["loggedIn"] is False
    assert status["username"] == ""


@pytest.mark.asyncio
async def test_cli_logout_uses_official_logout_command(monkeypatch) -> None:
    cli = BdpanCli(Settings())
    commands: list[list[str]] = []

    async def execute(
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        require_json: bool = False,
    ) -> BdpanRunResult:
        del stdin, require_json
        commands.append(argv)
        assert timeout == 30
        return BdpanRunResult(0, "已退出登录", "")

    monkeypatch.setattr(cli, "execute", execute)
    await cli.logout("bdpan")

    assert commands == [["bdpan", "logout", "--no-check-update"]]


@pytest.mark.asyncio
async def test_automation_logout_disables_following_and_keeps_watch_state() -> None:
    settings = Settings(bdpan_binary="bdpan")
    cli = FakeBdpanCli(settings)
    repository = FakeRuntimeRepository()
    service = BdpanAutomationService(
        settings,
        cli,
        repository,  # type: ignore[arg-type]
        FakeMedia(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    service.config["enabled"] = True
    service.states["m1"] = {"initialized": True, "seen": ["episode-1"]}

    result = await service.logout()

    assert cli.logout_calls == 1
    assert service.config["enabled"] is False
    assert repository.config == service.config
    assert service.states["m1"]["seen"] == ["episode-1"]
    assert result["config"]["enabled"] is False
    assert result["runtime"]["loggedIn"] is False


@pytest.mark.asyncio
async def test_cli_quota_decrypts_config_token_and_calls_official_api(
    monkeypatch, tmp_path: Path
) -> None:
    token = "test-access-token"
    key = bytes(range(32))
    nonce = bytes(range(12))
    encrypted = nonce + AESGCM(key).encrypt(nonce, token.encode(), None)
    encoded = base64.urlsafe_b64encode(encrypted).decode().rstrip("=")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"auth": {"access_token": f"enc:v1:{encoded}"}}),
        encoding="utf-8",
    )
    (tmp_path / ".token_key").write_text(key.hex() + "\n", encoding="ascii")
    monkeypatch.setenv("BDPAN_CONFIG_PATH", str(config))

    async def api(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/quota"
        assert request.url.params["access_token"] == token
        assert request.url.params["checkfree"] == "1"
        assert request.url.params["checkexpire"] == "1"
        return httpx.Response(200, json={"errno": 0, "total": 1_000, "used": 250})

    async with httpx.AsyncClient(
        base_url="https://pan.baidu.com/", transport=httpx.MockTransport(api)
    ) as client:
        quota = await BdpanCli(Settings(), client).quota()

    assert quota == {
        "available": True,
        "supported": True,
        "totalBytes": 1_000,
        "usedBytes": 250,
        "freeBytes": 750,
        "usedPercent": 25.0,
        "source": "bdpan 配置 · 百度开放 API",
        "error": "",
    }


@pytest.mark.asyncio
async def test_cli_quota_accepts_plain_config_token(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"auth": {"access_token": "plain-access-token"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BDPAN_CONFIG_PATH", str(tmp_path))

    async def api(request: httpx.Request) -> httpx.Response:
        assert request.url.params["access_token"] == "plain-access-token"
        return httpx.Response(200, json={"errno": 0, "total": 2_000, "used": 500})

    async with httpx.AsyncClient(
        base_url="https://pan.baidu.com/", transport=httpx.MockTransport(api)
    ) as client:
        quota = await BdpanCli(Settings(), client).quota()

    assert quota["available"] is True
    assert quota["totalBytes"] == 2_000
    assert quota["usedBytes"] == 500


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


@pytest.mark.asyncio
async def test_inspect_and_import_share_creates_media_and_pending_sync() -> None:
    settings = Settings(bdpan_binary="bdpan", bdpan_save_root="StrmFlow")
    cli = FakeBdpanCli(settings)
    repository = FakeRuntimeRepository()
    media = FakeImportMedia()
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
    shared_files = [
        media_file("1001", "Jiao.锋 (2026)/Season 01/Jiao.S01E01.mp4"),
        media_file("1002", "Jiao.锋 (2026)/Season 01/Jiao.S01E02.mp4"),
    ]

    async def list_share_media(
        _share_url: str,
        _extract_code: str,
        _session_id: str,
        *,
        strip_wrapper: bool = True,
    ) -> list[ShareMediaFile]:
        return (
            BdpanAutomationService._strip_single_wrapper(shared_files)
            if strip_wrapper
            else shared_files
        )

    service._list_share_media = list_share_media  # type: ignore[method-assign]

    preview = await service.inspect_share("https://pan.baidu.com/s/example?pwd=ab12")

    assert preview["fileCount"] == 2
    assert preview["candidateCount"] == 1
    assert preview["candidates"][0]["name"] == "Jiao.锋 (2026)"
    assert preview["candidates"][0]["title"] == "Jiao.锋"
    assert preview["candidates"][0]["year"] == "2026"
    assert "fsid" not in json.dumps(preview)

    result = await service.import_share(
        BdpanShareImportRequest(
            preview_id=preview["previewId"],
            candidate_id=preview["candidates"][0]["id"],
            type_dir="TV",
            category="国产剧",
            title="交锋",
            year="2026",
            total_episodes=24,
            season=1,
        )
    )

    assert result["submittedCount"] == 2
    assert result["taskCount"] == 1
    assert result["item"]["sourcePath"] == "/temp_strm/电视剧/国产剧/交锋 (2026)"
    assert media.saved.source_path == "/temp_strm/电视剧/国产剧/交锋 (2026)"
    assert media.saved.baidu_link == "https://pan.baidu.com/s/example?pwd=ab12"
    command = cli.executed[0]
    assert command[command.index("--fsid") + 1] == "1001,1002"
    assert command[command.index("-d") + 1] == ("StrmFlow/电视剧/国产剧/交锋 (2026)/Season 01")
    assert service.states["m2"]["watchPrefix"] == "Jiao.锋 (2026)"
    assert service.states["m2"]["pendingSyncAt"]
    assert service.states["m2"]["discoverParentBeforeSync"] is True
    pending_at = datetime.fromisoformat(service.states["m2"]["pendingSyncAt"])
    first_retry_at = datetime.fromisoformat(service.states["m2"]["firstRetrySyncAt"])
    assert abs((datetime.now(UTC) - pending_at).total_seconds()) < 5
    assert first_retry_at > pending_at
    assert repository.states == service.states


def test_share_candidates_separate_multiple_top_level_media() -> None:
    candidates = BdpanAutomationService._share_candidates(
        [
            media_file("1001", "甲剧 (2025)/Season 01/甲剧.S01E01.mp4"),
            media_file("1002", "乙剧 (2026)/Season 01/乙剧.S01E01.mp4"),
        ]
    )

    assert [candidate["name"] for candidate in candidates] == ["乙剧 (2026)", "甲剧 (2025)"]
    assert all(candidate["fileCount"] == 1 for candidate in candidates)


def test_share_candidates_deduplicate_same_episode_and_keep_quality_variant() -> None:
    candidates = BdpanAutomationService._share_candidates(
        [
            media_file("1001", "百花杀 (2026)/S01E06 1080p.mp4"),
            media_file("1002", "百花杀 (2026)/S01E06 4K.mp4"),
            media_file("1003", "百花杀 (2026)/S01E06 4K(1).mp4"),
            media_file("1004", "百花杀 (2026)/S01E07 4K.mp4"),
        ]
    )
    assert candidates[0]["fileCount"] == 2
    assert candidates[0]["duplicateCount"] == 2
    assert candidates[0]["sampleFiles"] == ["S01E06 4K.mp4", "S01E07 4K.mp4"]


def test_share_candidates_never_transfer_strm_reference_files() -> None:
    candidates = BdpanAutomationService._share_candidates(
        [
            media_file("1001", "交锋 (2026)/S01E01 4K.strm"),
            media_file("1002", "交锋 (2026)/S01E01 4K.mp4"),
        ]
    )

    assert candidates[0]["fileCount"] == 1
    assert candidates[0]["sampleFiles"] == ["S01E01 4K.mp4"]


@pytest.mark.asyncio
async def test_source_inventory_uses_underlying_videos_not_generated_strm_view() -> None:
    settings = Settings(bdpan_binary="bdpan")
    media = FakeUnderlyingMedia(["S01E01 4KHDR60FPS.strm", "S01E02 4KHDR60FPS.mp4"])
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        FakeRuntimeRepository(),  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        FakeOpenList(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
        storage=FakeSourceStorage(),  # type: ignore[arg-type]
    )

    missing, upgrades, checked = await service._saved_episode_repairs(
        media.item,
        [
            media_file("1001", "S01E01 4KHDR60FPS.mp4"),
            media_file("1002", "S01E02 4KHDR60FPS.mp4"),
        ],
        {},
    )

    assert checked is True
    assert [file.name for file in missing] == ["S01E01 4KHDR60FPS.mp4"]
    assert upgrades == []


@pytest.mark.asyncio
async def test_cleanup_removes_only_strm_shadowed_by_equal_or_better_video() -> None:
    settings = Settings(bdpan_binary="bdpan")
    media = FakeUnderlyingMedia(
        [
            "S01E01 4KHDR60FPS.strm",
            "S01E01 4KHDR60FPS.mp4",
            "S01E02 4KHDR.strm",
            "S01E02 1080p.mp4",
        ]
    )
    openlist = FakeOpenList()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        FakeRuntimeRepository(),  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        openlist,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
        storage=FakeSourceStorage(),  # type: ignore[arg-type]
    )

    removed = await service._cleanup_shadowed_source_manifests(media.item)

    assert removed == 1
    assert openlist.requests == [
        (
            "REMOVE",
            "/bdpan/apps/bdpan/media/TV/国产剧/交锋 (2026)",
            {"names": ["S01E01 4KHDR60FPS.strm"]},
        )
    ]


def test_episode_repairs_restore_missing_and_upgrade_inferior_saved_variants() -> None:
    shared = [
        media_file("1001", "S01E04 4KHDR60FPS-GyWEB.mp4", size=1_200),
        media_file("1002", "S01E05 4KHDR60FPS-GyWEB.mp4", size=1_300),
        media_file("1003", "S01E06 4KHDR60FPS-GyWEB.mp4", size=1_100),
    ]
    saved = [
        "S01E04 4K60FPS-GyWEB.strm",
        "S01E05 4KHDR60FPS-GyWEB.strm",
    ]

    missing, upgrades = BdpanAutomationService._episode_repairs(shared, saved)

    assert [file.name for file in missing] == ["S01E06 4KHDR60FPS-GyWEB.mp4"]
    assert [file.name for file in upgrades] == ["S01E04 4KHDR60FPS-GyWEB.mp4"]


def test_pending_transfer_requires_every_selected_episode_and_quality_to_land() -> None:
    expected = [
        "S01E04 4KHDR60FPS-GyWEB.mp4",
        "S01E05 4KHDR60FPS-GyWEB.mp4",
        "S01E06 4KHDR60FPS-GyWEB.mp4",
    ]
    saved = [
        "S01E04 4K60FPS-GyWEB.strm",
        "S01E05 4KHDR60FPS-GyWEB.strm",
    ]

    assert BdpanAutomationService._pending_missing_files(expected, saved) == [
        "S01E04 4KHDR60FPS-GyWEB.mp4",
        "S01E06 4KHDR60FPS-GyWEB.mp4",
    ]


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


@pytest.mark.asyncio
async def test_immediate_share_sync_waits_without_error_when_transfer_directory_is_not_ready() -> (
    None
):
    settings = Settings(bdpan_binary="bdpan")
    media = FakeMedia()
    openlist = FakeOpenList()
    logs = RuntimeLogStore()
    repository = FakeRuntimeRepository()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        repository,  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        openlist,  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        logs,
    )
    retry_at = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
    service.states["m1"] = {
        "pendingSyncAttempts": 0,
        "pendingSyncAt": datetime.now(UTC).isoformat(),
        "firstRetrySyncAt": retry_at,
        "discoverParentBeforeSync": True,
        "pendingFiles": ["S01E01 4K.mp4"],
    }

    async def missing_directory(
        _method: str, _path: str, _body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise AppError(502, "OpenList 请求失败：failed get objs: failed get dir: object not found")

    openlist.request = missing_directory  # type: ignore[method-assign]

    await service._sync_item("m1")

    state = service.states["m1"]
    assert state["pendingSyncAt"] == retry_at
    assert state["pendingSyncAttempts"] == 0
    assert state["lastError"] == ""
    assert "firstRetrySyncAt" not in state
    assert media.published == []
    assert openlist.requests == []
    assert any(
        entry["level"] == "info" and "转存目录尚未落盘" in entry["message"] for entry in logs.list()
    )


@pytest.mark.asyncio
async def test_initial_share_sync_scans_parent_to_discover_transferred_media_directory() -> None:
    settings = Settings(bdpan_binary="bdpan")
    media = FakeMedia()
    openlist = FakeOpenList()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        FakeRuntimeRepository(),  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        openlist,  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    service.states["m1"] = {
        "pendingSyncAttempts": 0,
        "pendingSyncAt": "now",
        "discoverParentBeforeSync": True,
    }

    await service._sync_item("m1")

    assert openlist.requests[0] == (
        "POST",
        "/api/admin/scan/start",
        {"path": "/temp_strm/TV/国产剧", "limit": settings.scan_limit},
    )
    assert media.published == ["m1"]
    assert "discoverParentBeforeSync" not in service.states["m1"]


@pytest.mark.asyncio
async def test_pending_transfer_finishes_when_user_already_synchronized_files() -> None:
    settings = Settings(bdpan_binary="bdpan")
    media = FakeAlreadySyncedMedia()
    repository = FakeRuntimeRepository()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        repository,  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        FakeOpenList(),  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    service.states["m1"] = {
        "pendingSyncAttempts": 2,
        "pendingSyncAt": "now",
        "lastResult": "转存已提交，等待网盘文件落盘",
    }

    await service._sync_item("m1")

    state = service.states["m1"]
    assert state["pendingSyncAt"] is None
    assert state["pendingSyncAttempts"] == 0
    assert state["lastResult"] == "转存落盘并同步完成，当前 3 集"
    assert state["lastSyncedAt"]


@pytest.mark.asyncio
async def test_pending_transfer_sends_episode_update_notification() -> None:
    settings = Settings(bdpan_binary="bdpan")
    media = FakeAlreadySyncedMedia()
    notifications = FakeNotifications()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        FakeRuntimeRepository(),  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        FakeOpenList(),  # type: ignore[arg-type]
        FakeEmby(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
        notifications,  # type: ignore[arg-type]
    )
    service.states["m1"] = {
        "pendingSyncAttempts": 0,
        "pendingSyncAt": "now",
        "pendingNotificationNewCount": 2,
        "pendingNotificationEpisodes": ["S01E02", "S01E03"],
    }

    await service._sync_item("m1")

    assert notifications.episode_updates == [("m1", 2, 3, ["S01E02", "S01E03"])]
    assert "pendingNotificationNewCount" not in service.states["m1"]
    assert "pendingNotificationEpisodes" not in service.states["m1"]


@pytest.mark.asyncio
async def test_invalid_share_link_notifies_only_once_until_it_recovers() -> None:
    settings = Settings(bdpan_binary="bdpan")
    notifications = FakeNotifications()
    service = BdpanAutomationService(
        settings,
        FakeBdpanCli(settings),
        FakeRuntimeRepository(),  # type: ignore[arg-type]
        FakeMedia(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
        RuntimeLogStore(),
        notifications,  # type: ignore[arg-type]
    )

    async def invalid_share(*_args: Any, **_kwargs: Any) -> list[ShareMediaFile]:
        raise BdpanCliError("分享链接已失效、已取消或不存在", code="13004")

    service._list_share_media = invalid_share  # type: ignore[method-assign]
    for _ in range(2):
        with pytest.raises(BdpanCliError):
            await service.check_item("m1")

    assert notifications.invalid_links == [("m1", "分享链接已失效、已取消或不存在")]
