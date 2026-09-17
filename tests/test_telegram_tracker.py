from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.services.telegram_tracker import TelegramTrackerService


class FakeRuntimeRepository:
    def __init__(self) -> None:
        self.telegram: dict[str, Any] | None = None

    async def load_telegram(self) -> dict[str, Any] | None:
        return self.telegram

    async def save_telegram(self, value: dict[str, Any]) -> None:
        self.telegram = value


class FakeMedia:
    def __init__(self) -> None:
        self.items = [
            {
                "id": "world",
                "name": "完美世界",
                "title": "完美世界",
                "year": "",
                "season": 1,
                "status": "ongoing",
                "baiduLink": "https://pan.baidu.com/s/expired",
            },
            {
                "id": "zhuxian",
                "name": "诛仙 (2026)",
                "title": "诛仙",
                "year": "2026",
                "season": 4,
                "status": "ongoing",
                "baiduLink": "",
            },
            {
                "id": "finished",
                "name": "完结媒体",
                "title": "完结媒体",
                "season": 1,
                "status": "completed",
                "baiduLink": "",
            },
        ]
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def list_items(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.items]

    async def update_item(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((item_id, patch))
        item = next(item for item in self.items if item["id"] == item_id)
        item.update(patch)
        return dict(item)


class FakeBdpan:
    def __init__(self) -> None:
        self.config = {"trackingMode": "hybrid"}
        self.updates: list[dict[str, Any]] = []

    async def telegram_update(self, item: dict[str, Any], **details: Any) -> None:
        self.updates.append({"item": dict(item), **details})

    @staticmethod
    def _iso_now() -> str:
        return "2026-09-17T12:00:00+00:00"


MESSAGE = """
完美世界 动漫 4K臻彩 更新287集
https://pan.quark.cn/s/0c26a4a40e7f
https://pan.baidu.com/s/1RpfLpYsh8XSm9y_yP64R5w?pwd=6666
https://www.guangyapan.com/s/example
诛仙 第四季 (2026)4K 更新07集
https://pan.quark.cn/s/612d287afb30
https://pan.baidu.com/s/1fjjH56lIXiOXF9xW-6t_vg?pwd=6666
https://www.guangyapan.com/s/example2

📢 频道 https://t.me/WFYSFX03
"""


def build_service() -> tuple[TelegramTrackerService, FakeMedia, FakeBdpan]:
    media = FakeMedia()
    bdpan = FakeBdpan()
    service = TelegramTrackerService(
        Settings(
            _env_file=None,
            session_secret="test-session-secret-that-is-long-enough-123456",
            telegram_api_id=123456,
            telegram_api_hash="0123456789abcdef0123456789abcdef",
        ),
        FakeRuntimeRepository(),  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        bdpan,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )
    return service, media, bdpan


def test_parse_channel_message_into_media_blocks() -> None:
    candidates = TelegramTrackerService.parse_message(MESSAGE)

    assert len(candidates) == 2
    assert candidates[0].episode == 287
    assert candidates[0].season is None
    assert candidates[0].baidu_link == ("https://pan.baidu.com/s/1RpfLpYsh8XSm9y_yP64R5w?pwd=6666")
    assert candidates[1].episode == 7
    assert candidates[1].season == 4
    assert candidates[1].year == "2026"


async def test_process_message_matches_ongoing_media_and_replaces_links() -> None:
    service, media, bdpan = build_service()

    matched = await service.process_message(MESSAGE, source="@wfysfx03")

    assert matched == 2
    assert [item_id for item_id, _patch in media.updates] == ["world", "zhuxian"]
    assert [update["episode"] for update in bdpan.updates] == [287, 7]
    assert all(update["link_changed"] for update in bdpan.updates)
    assert all(update["source"] == "@wfysfx03" for update in bdpan.updates)


def test_normalize_telegram_sources() -> None:
    assert TelegramTrackerService._normalize_source("https://t.me/WFYSFX03/") == "@wfysfx03"
    assert TelegramTrackerService._normalize_source("-1001234567890") == "-1001234567890"
    assert TelegramTrackerService._normalize_source("not valid!") == ""


async def test_status_masks_telegram_secrets() -> None:
    service, _media, _bdpan = build_service()
    service.config.update(
        {
            "phone": "+8613800000000",
            "session": "sensitive-session",
        }
    )

    status = await service.status()

    assert status["config"]["apiConfigured"] is True
    assert status["config"]["phoneConfigured"] is True
    assert status["config"]["phoneMasked"] == "+86****000"
    assert "apiHash" not in status["config"]
    assert "session" not in status["config"]
    assert status["runtime"]["authorized"] is True


def test_telegram_api_credentials_are_secret_environment_settings() -> None:
    api_hash = "0123456789abcdef0123456789abcdef"
    settings = Settings(
        _env_file=None,
        telegram_api_id=123456,
        telegram_api_hash=api_hash,
    )

    assert settings.telegram_api_hash.get_secret_value() == api_hash
    assert api_hash not in repr(settings)
    with pytest.raises(ValidationError, match="必须同时配置"):
        Settings(_env_file=None, telegram_api_id=123456)
    invalid_hash = "must-not-appear-in-errors"
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            session_secret="test-session-secret-that-is-long-enough-123456",
            telegram_api_id=123456,
            telegram_api_hash=invalid_hash,
        )
    assert invalid_hash not in str(error.value)


async def test_initialize_removes_legacy_api_credentials_from_sqlite_config() -> None:
    repository = FakeRuntimeRepository()
    repository.telegram = {
        "enabled": False,
        "apiId": 123456,
        "apiHash": "legacy-secret",
        "phone": "",
        "sources": ["@wfysfx03"],
        "session": "legacy-plaintext-session",
    }
    media = FakeMedia()
    bdpan = FakeBdpan()
    service = TelegramTrackerService(
        Settings(
            _env_file=None,
            session_secret="test-session-secret-that-is-long-enough-123456",
            telegram_api_id=123456,
            telegram_api_hash="0123456789abcdef0123456789abcdef",
        ),
        repository,  # type: ignore[arg-type]
        media,  # type: ignore[arg-type]
        bdpan,  # type: ignore[arg-type]
        RuntimeLogStore(),
    )

    await service.initialize()

    assert repository.telegram is not None
    assert "apiId" not in repository.telegram
    assert "apiHash" not in repository.telegram
    assert "session" not in repository.telegram
    assert repository.telegram["sessionEncrypted"].startswith("v1.")
    assert "legacy-plaintext-session" not in repository.telegram["sessionEncrypted"]
