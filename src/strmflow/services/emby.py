from __future__ import annotations

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
