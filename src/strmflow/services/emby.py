from __future__ import annotations

from typing import Any

import httpx

from strmflow.core.config import Settings
from strmflow.core.errors import AppError, UpstreamError


class EmbyClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http

    async def refresh_library(self) -> None:
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(500, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        try:
            response = await self.http.post(
                self.settings.emby_refresh_path,
                headers={"X-Emby-Token": self.settings.emby_api_key, "Accept": "application/json"},
                timeout=30,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError("Emby 刷新超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("无法连接 Emby") from exc
        if not response.is_success:
            raise UpstreamError(
                f"Emby 刷新失败：HTTP {response.status_code} - {response.text[:500]}"
            )

    async def system_info(self, *, timeout: float = 10) -> dict[str, Any]:
        """Read authenticated Emby server information for health reporting."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(409, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        try:
            response = await self.http.get(
                "System/Info",
                headers={
                    "X-Emby-Token": self.settings.emby_api_key,
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError("Emby 状态检查超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("无法连接 Emby") from exc
        if not response.is_success:
            raise UpstreamError(f"Emby 状态检查失败：HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("Emby 状态接口返回了无效数据") from exc
        if not isinstance(payload, dict):
            raise UpstreamError("Emby 状态接口返回了无效数据")
        return payload
