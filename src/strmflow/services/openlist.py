from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from strmflow.core.config import Settings
from strmflow.core.errors import AppError, UpstreamError
from strmflow.utils.paths import normalize_virtual_path


class OpenListClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http

    async def request(
        self,
        method: str,
        pathname: str,
        body: dict[str, Any] | None = None,
        *,
        base_url: str = "",
        timeout: float | None = None,
        user_agent: str = "",
    ) -> Any:
        headers = {"Authorization": self.settings.openlist_token, "Accept": "application/json"}
        if user_agent:
            headers["User-Agent"] = user_agent
        request_url = f"{base_url.rstrip('/')}/{pathname.lstrip('/')}" if base_url else pathname
        try:
            response = await self.http.request(
                method,
                request_url,
                headers=headers,
                json=body,
                timeout=timeout if timeout is not None else self.settings.openlist_timeout,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError("OpenList 请求超时", {"service": "OpenList"}, 504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("无法连接 OpenList", {"service": "OpenList"}) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(f"OpenList 返回非 JSON 响应：{response.text[:200]}") from exc
        if not response.is_success or payload.get("code") != 200:
            message = payload.get("message") or f"HTTP {response.status_code}"
            status = 409 if response.status_code == 400 else 502
            raise UpstreamError(
                f"OpenList 请求失败：{message}",
                {
                    "service": "OpenList",
                    "httpStatus": response.status_code,
                    "code": payload.get("code"),
                },
                status,
            )
        return payload.get("data")

    async def list_dir(self, path: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        data = await self.request(
            "POST",
            "/api/fs/list",
            {
                "path": normalize_virtual_path(path),
                "password": self.settings.openlist_path_password,
                "page": 1,
                "per_page": 1000,
                "refresh": refresh,
            },
        )
        content = data.get("content") if isinstance(data, dict) else None
        return content if isinstance(content, list) else []

    async def mkdir(self, path: str) -> None:
        normalized = normalize_virtual_path(path)
        try:
            await self.request("POST", "/api/fs/mkdir", {"path": normalized})
        except AppError as exc:
            # OpenList may report an existing directory as a conflict.
            if exc.status_code != 409:
                raise

    async def get_file_info(
        self,
        path: str,
        *,
        base_url: str = "",
        timeout: float | None = None,
        user_agent: str = "",
    ) -> dict[str, Any]:
        data = await self.request(
            "POST",
            "/api/fs/get",
            {
                "path": normalize_virtual_path(path),
                "password": self.settings.openlist_path_password,
            },
            base_url=base_url,
            timeout=timeout,
            user_agent=user_agent,
        )
        if not isinstance(data, dict):
            raise AppError(404, f"文件不存在：{path}")
        return data

    async def read_text(
        self,
        path: str,
        *,
        base_url: str = "",
        timeout: float | None = None,
    ) -> str:
        normalized = normalize_virtual_path(path)
        # 部分 OpenList 版本会为不存在的文件生成 /api/fs/link URL，随后访问该
        # URL 才返回 HTTP 500。先读取文件元数据，明确区分“不存在”和上游故障。
        try:
            await self.request(
                "POST",
                "/api/fs/get",
                {"path": normalized, "password": self.settings.openlist_path_password},
                base_url=base_url,
                timeout=timeout,
            )
        except AppError as exc:
            if (
                "object not found" in exc.message.casefold()
                or "not found" in exc.message.casefold()
            ):
                raise AppError(404, "配置文件不存在") from exc
            raise

        data = await self.request(
            "POST",
            "/api/fs/link",
            {"path": normalized},
            base_url=base_url,
            timeout=timeout,
        )
        url = data.get("url") or data.get("URL") if isinstance(data, dict) else None
        if not url:
            raise AppError(404, "配置文件不存在")
        try:
            response = await self.http.get(url, timeout=timeout or self.settings.openlist_timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise AppError(404, "配置文件不存在") from exc
            raise UpstreamError(f"读取配置文件失败：HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("读取配置文件失败") from exc
        return response.text

    async def write_text(self, path: str, text: str) -> None:
        normalized = normalize_virtual_path(path)
        try:
            response = await self.http.put(
                "/api/fs/put",
                headers={
                    "Authorization": self.settings.openlist_token,
                    "File-Path": quote(normalized, safe="/"),
                    "Content-Type": "application/json; charset=utf-8",
                    "Overwrite": "true",
                },
                content=text.encode(),
                timeout=self.settings.openlist_timeout,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError("保存配置文件失败") from exc
        if not response.is_success or payload.get("code") != 200:
            raise UpstreamError(f"保存配置文件失败：{payload.get('message', response.status_code)}")

    async def copy(self, source_dir: str, target_dir: str, names: list[str]) -> None:
        await self.request(
            "POST",
            "/api/fs/copy",
            {
                "src_dir": normalize_virtual_path(source_dir),
                "dst_dir": normalize_virtual_path(target_dir),
                "names": names,
                "overwrite": True,
                "skip_existing": False,
                "merge": True,
            },
        )

    async def remove(self, directory: str, names: list[str]) -> None:
        if not names:
            return
        await self.request(
            "POST",
            "/api/fs/remove",
            {
                "dir": normalize_virtual_path(directory),
                "names": names,
            },
        )

    async def batch_rename(self, directory: str, changes: list[dict[str, str]]) -> None:
        await self.request(
            "POST",
            "/api/fs/batch_rename",
            {"src_dir": normalize_virtual_path(directory), "rename_objects": changes},
        )
