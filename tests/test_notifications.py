from __future__ import annotations

from typing import Any

import httpx
import pytest

from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.schemas.api import WecomWebhookConfigUpdate
from strmflow.services.notifications import WecomWebhookService

WEBHOOK = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=12345678-1234-1234-1234-123456789abc"
)


class FakeNotificationRepository:
    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    async def load_wecom_webhook(self) -> dict[str, Any] | None:
        return self.config

    async def save_wecom_webhook(self, value: dict[str, Any]) -> None:
        self.config = dict(value)


@pytest.mark.asyncio
async def test_wecom_config_is_persisted_but_secret_is_masked() -> None:
    repository = FakeNotificationRepository()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WecomWebhookService(
            repository,  # type: ignore[arg-type]
            client,
            RuntimeLogStore(),
        )
        status = await service.update_config(
            WecomWebhookConfigUpdate(
                webhook_url=WEBHOOK,
                episode_update_enabled=True,
                link_invalid_enabled=True,
            )
        )
        sent = await service.send_test()

    assert repository.config == {
        "webhookUrl": WEBHOOK,
        "episodeUpdateEnabled": True,
        "linkInvalidEnabled": True,
    }
    assert status["config"]["webhookConfigured"] is True
    assert status["config"]["maskedWebhookUrl"].endswith("••••9abc")
    assert WEBHOOK not in str(status)
    assert sent["sent"] is True
    assert len(requests) == 1
    assert requests[0].url == WEBHOOK
    assert b'"msgtype":"text"' in requests[0].content
    assert "🔔 StrmFlow 通知测试" in requests[0].content.decode()


@pytest.mark.asyncio
async def test_wecom_update_can_retain_or_clear_saved_webhook() -> None:
    repository = FakeNotificationRepository()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        service = WecomWebhookService(
            repository,  # type: ignore[arg-type]
            client,
            RuntimeLogStore(),
        )
        await service.update_config(WecomWebhookConfigUpdate(webhook_url=WEBHOOK))
        await service.update_config(
            WecomWebhookConfigUpdate(
                webhook_url=None,
                episode_update_enabled=True,
            )
        )
        assert repository.config is not None
        assert repository.config["webhookUrl"] == WEBHOOK

        masked = service.status()["config"]["maskedWebhookUrl"]
        await service.update_config(
            WecomWebhookConfigUpdate(
                webhook_url=masked,
                episode_update_enabled=True,
                link_invalid_enabled=True,
            )
        )
        assert repository.config["webhookUrl"] == WEBHOOK

        status = await service.update_config(WecomWebhookConfigUpdate(webhook_url=""))

    assert status["config"]["webhookConfigured"] is False
    assert repository.config is not None
    assert repository.config["webhookUrl"] == ""


@pytest.mark.asyncio
async def test_episode_update_notification_lists_exact_episode() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = WecomWebhookService(
            FakeNotificationRepository(),  # type: ignore[arg-type]
            client,
            RuntimeLogStore(),
        )
        await service.update_config(
            WecomWebhookConfigUpdate(
                webhook_url=WEBHOOK,
                episode_update_enabled=True,
            )
        )
        sent = await service.notify_episode_update(
            {"name": "兰香如故 (2026)"},
            1,
            13,
            ["S01E13"],
        )

    assert sent is True
    content = requests[0].content.decode()
    assert "🆕 本次新增：1 集" in content
    assert "🎞️ 新增剧集：第 13 集" in content
    assert "📚 当前已同步：13 集" in content


def test_episode_update_detail_keeps_multi_season_identity() -> None:
    assert (
        WecomWebhookService._format_episode_detail(["S02E01", "S01E13", "S02E01", "invalid"])
        == "第 13 集、S02E01"
    )


@pytest.mark.asyncio
async def test_wecom_rejects_non_official_webhook_url() -> None:
    async with httpx.AsyncClient() as client:
        service = WecomWebhookService(
            FakeNotificationRepository(),  # type: ignore[arg-type]
            client,
            RuntimeLogStore(),
        )
        with pytest.raises(AppError, match="qyapi.weixin.qq.com"):
            await service.update_config(
                WecomWebhookConfigUpdate(
                    webhook_url="https://example.test/cgi-bin/webhook/send?key=1234567890123456"
                )
            )
