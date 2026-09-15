from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.schemas.api import WecomWebhookConfigUpdate

WEBHOOK_PATH = "/cgi-bin/webhook/send"
WEBHOOK_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class WecomWebhookService:
    """Persist and deliver Enterprise WeChat group-bot notifications."""

    def __init__(
        self,
        repository: RuntimeSettingsRepository,
        http: httpx.AsyncClient,
        runtime_logs: RuntimeLogStore,
    ) -> None:
        self.repository = repository
        self.http = http
        self.runtime_logs = runtime_logs
        self.config = self._default_config()
        self.last_sent_at = ""
        self.last_event = ""
        self.last_error = ""

    async def initialize(self) -> None:
        stored = await self.repository.load_wecom_webhook()
        if stored:
            try:
                self.config = self._normalize_config(stored, retain_url=False)
            except AppError:
                self._log("warning", "已忽略格式不正确的企业微信 Webhook 配置")

    def status(self) -> dict[str, Any]:
        url = str(self.config.get("webhookUrl") or "")
        return {
            "config": {
                "webhookConfigured": bool(url),
                "maskedWebhookUrl": self._masked_url(url),
                "episodeUpdateEnabled": bool(self.config["episodeUpdateEnabled"]),
                "linkInvalidEnabled": bool(self.config["linkInvalidEnabled"]),
            },
            "runtime": {
                "lastSentAt": self.last_sent_at or None,
                "lastEvent": self.last_event,
                "lastError": self.last_error,
            },
        }

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "webhookUrl": "",
            "episodeUpdateEnabled": False,
            "linkInvalidEnabled": False,
        }

    async def update_config(self, body: WecomWebhookConfigUpdate) -> dict[str, Any]:
        incoming = body.model_dump(by_alias=True)
        self.config = self._normalize_config(incoming, retain_url=True)
        await self.repository.save_wecom_webhook(self.config)
        self._log(
            "success",
            "企业微信 Webhook 通知配置已更新",
            configured=bool(self.config["webhookUrl"]),
            episodeUpdateEnabled=self.config["episodeUpdateEnabled"],
            linkInvalidEnabled=self.config["linkInvalidEnabled"],
        )
        return self.status()

    async def send_test(self) -> dict[str, Any]:
        if not self.config["webhookUrl"]:
            raise AppError(409, "请先保存企业微信机器人 Webhook 地址")
        await self._send(
            "test",
            "### StrmFlow 通知测试\n"
            '> 状态：<font color="info">Webhook 配置有效</font>\n'
            f"> 时间：{self._local_time()}",
            raise_error=True,
        )
        return {"sent": True, "message": "测试通知已发送"}

    async def notify_episode_update(
        self,
        item: dict[str, Any],
        new_count: int,
        current_count: int,
    ) -> bool:
        if not self.config["webhookUrl"] or not self.config["episodeUpdateEnabled"]:
            return False
        name = self._safe_text(item.get("name") or item.get("title") or "未命名媒体")
        content = (
            "### StrmFlow 剧集更新\n"
            f"> 媒体：**{name}**\n"
            f'> 本次新增：<font color="info">{max(0, int(new_count))} 集</font>\n'
            f"> 当前已同步：{max(0, int(current_count))} 集\n"
            f"> 时间：{self._local_time()}"
        )
        return await self._send("episode_update", content)

    async def notify_link_invalid(self, item: dict[str, Any], error: Exception) -> bool:
        if not self.config["webhookUrl"] or not self.config["linkInvalidEnabled"]:
            return False
        name = self._safe_text(item.get("name") or item.get("title") or "未命名媒体")
        reason = self._safe_text(str(error)[:200] or "分享链接已失效")
        content = (
            "### StrmFlow 分享链接失效\n"
            f"> 媒体：**{name}**\n"
            f'> 状态：<font color="warning">需要更换分享链接</font>\n'
            f"> 原因：{reason}\n"
            f"> 时间：{self._local_time()}"
        )
        return await self._send("link_invalid", content)

    async def _send(self, event: str, content: str, *, raise_error: bool = False) -> bool:
        try:
            response = await self.http.post(
                str(self.config["webhookUrl"]),
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("企业微信返回格式异常")
            if int(payload.get("errcode", -1)) != 0:
                message = str(payload.get("errmsg") or "企业微信返回未知错误")
                raise RuntimeError(message)
            self.last_sent_at = datetime.now().astimezone().isoformat()
            self.last_event = event
            self.last_error = ""
            self._log("success", f"企业微信通知已发送：{event}")
            return True
        except Exception as exc:
            message = self._safe_error(exc)
            self.last_error = message
            self._log("error", f"企业微信通知发送失败：{message}", event=event)
            if raise_error:
                raise AppError(502, f"企业微信通知发送失败：{message}") from exc
            return False

    def _normalize_config(self, value: dict[str, Any], *, retain_url: bool) -> dict[str, Any]:
        raw_url = value.get("webhookUrl")
        if raw_url is None and retain_url:
            webhook_url = str(self.config.get("webhookUrl") or "")
        else:
            webhook_url = str(raw_url or "").strip()
        if webhook_url:
            self._validate_url(webhook_url)
        return {
            "webhookUrl": webhook_url,
            "episodeUpdateEnabled": bool(value.get("episodeUpdateEnabled")),
            "linkInvalidEnabled": bool(value.get("linkInvalidEnabled")),
        }

    @staticmethod
    def _validate_url(value: str) -> None:
        try:
            parsed = urlsplit(value)
            query = parse_qs(parsed.query)
            port = parsed.port
        except ValueError as exc:
            raise AppError(400, "企业微信 Webhook 地址格式不正确") from exc
        keys = query.get("key") or []
        if (
            parsed.scheme != "https"
            or parsed.hostname != "qyapi.weixin.qq.com"
            or port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.path.rstrip("/") != WEBHOOK_PATH
            or parsed.fragment
            or len(keys) != 1
            or not WEBHOOK_KEY.fullmatch(keys[0])
        ):
            raise AppError(
                400,
                "请填写 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=... 格式的地址",
            )

    @staticmethod
    def _masked_url(value: str) -> str:
        if not value:
            return ""
        key = (parse_qs(urlsplit(value).query).get("key") or [""])[0]
        suffix = key[-4:] if key else ""
        return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=••••{suffix}"

    def _safe_error(self, error: Exception) -> str:
        message = str(error)[:300]
        if self.config.get("webhookUrl"):
            message = message.replace(
                str(self.config["webhookUrl"]), self._masked_url(str(self.config["webhookUrl"]))
            )
        return re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[已隐藏]", message)

    @staticmethod
    def _safe_text(value: object) -> str:
        return html.escape(str(value or "").replace("\n", " ").replace("\r", " "))

    @staticmethod
    def _local_time() -> str:
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S")

    def _log(self, level: str, message: str, **details: Any) -> None:
        self.runtime_logs.add(
            category="notification",
            level=level,
            message=message,
            **details,
        )
