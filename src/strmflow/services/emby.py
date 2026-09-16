from __future__ import annotations

from typing import Any

import httpx

from strmflow.core.config import Settings
from strmflow.core.errors import AppError, UpstreamError
from strmflow.utils.paths import normalize_virtual_path


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

    async def find_item_by_path(self, path: str, *, timeout: float = 10) -> dict[str, Any] | None:
        """Resolve the Emby item created for an exact STRM virtual path."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            return None
        try:
            response = await self.http.get(
                "/emby/Items",
                params={
                    "Path": path,
                    "Recursive": "true",
                    "Fields": "Path,MediaSources,MediaStreams",
                },
                headers={
                    "X-Emby-Token": self.settings.emby_api_key,
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        items = payload.get("Items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return None
        return next(
            (
                item
                for item in items
                if isinstance(item, dict)
                and (
                    str(item.get("Path") or "") == path
                    or any(
                        isinstance(source, dict) and str(source.get("Path") or "") == path
                        for source in item.get("MediaSources") or []
                    )
                )
            ),
            None,
        )

    async def extract_media_info(
        self,
        item_id: str,
        *,
        media_source_id: str = "",
        timeout: float = 90,
    ) -> dict[str, Any]:
        """Trigger Emby's native remote probe so Emby persists technical metadata itself."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(409, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        try:
            body: dict[str, Any] = {"IsPlayback": True}
            if media_source_id:
                body["MediaSourceId"] = media_source_id
            response = await self.http.post(
                f"/emby/Items/{item_id}/PlaybackInfo",
                json=body,
                headers={
                    "X-Emby-Token": self.settings.emby_api_key,
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise UpstreamError("Emby 媒体信息提取超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Emby 媒体信息提取请求失败") from exc
        except ValueError as exc:
            raise UpstreamError("Emby 媒体信息接口返回了无效数据") from exc
        if not isinstance(payload, dict):
            raise UpstreamError("Emby 媒体信息接口返回了无效数据")
        return payload

    async def list_strm_media_sources(
        self, root: str, *, timeout: float = 30
    ) -> list[dict[str, Any]]:
        """List STRM-backed movie/episode sources below the configured Emby root."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(409, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        normalized_root = normalize_virtual_path(root)
        prefix = normalized_root.rstrip("/") + "/"
        result: list[dict[str, Any]] = []
        start = 0
        limit = 500
        while True:
            try:
                response = await self.http.get(
                    "/emby/Items",
                    params={
                        "Recursive": "true",
                        "IncludeItemTypes": "Episode,Movie",
                        "Fields": "Path,MediaSources,MediaStreams",
                        "StartIndex": start,
                        "Limit": limit,
                    },
                    headers={
                        "X-Emby-Token": self.settings.emby_api_key,
                        "Accept": "application/json",
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.TimeoutException as exc:
                raise UpstreamError("Emby 缺失媒体信息扫描超时", status_code=504) from exc
            except httpx.HTTPError as exc:
                raise UpstreamError("Emby 缺失媒体信息扫描请求失败") from exc
            except ValueError as exc:
                raise UpstreamError("Emby 媒体列表返回了无效数据") from exc
            items = payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise UpstreamError("Emby 媒体列表返回了无效数据")
            for item in items:
                if not isinstance(item, dict) or not item.get("Id"):
                    continue
                # After Emby has successfully probed a STRM, MediaSources[].Path is
                # replaced by the resolved HTTP media URL.  Item.Path remains the
                # stable path of the managed .strm file, so prefer it when deciding
                # whether the item belongs to our target tree.  Looking only at the
                # media-source path made a completed item disappear from subsequent
                # scans and, more importantly, caused the whole scan to report zero
                # items as soon as Emby had resolved every source to an HTTP URL.
                item_path = str(item.get("Path") or "")
                normalized_item_path = (
                    normalize_virtual_path(item_path)
                    if item_path.casefold().endswith(".strm")
                    else ""
                )
                managed_item_path = (
                    normalized_item_path
                    if normalized_item_path
                    and (
                        normalized_item_path == normalized_root
                        or normalized_item_path.startswith(prefix)
                    )
                    else ""
                )
                sources = [
                    source for source in item.get("MediaSources") or [] if isinstance(source, dict)
                ]
                if not sources:
                    sources = [item]
                for source in sources:
                    normalized_path = managed_item_path
                    if not normalized_path:
                        source_path = str(source.get("Path") or "")
                        if not source_path.casefold().endswith(".strm"):
                            continue
                        normalized_path = normalize_virtual_path(source_path)
                        if normalized_path != normalized_root and not normalized_path.startswith(
                            prefix
                        ):
                            continue
                    source_id = str(source.get("Id") or "")
                    result.append(
                        {
                            "itemId": str(item["Id"]),
                            "mediaSourceId": source_id,
                            "path": normalized_path,
                            "hasMediaInfo": self.has_media_info(item, source_id),
                        }
                    )
            start += len(items)
            total = int(payload.get("TotalRecordCount") or start)
            if not items or start >= total:
                break
        # A grouped Emby item can expose more than one source for the same STRM
        # item.  It is sufficient for one of them to contain native media info;
        # retain that source rather than letting response order decide the result.
        by_path: dict[str, dict[str, Any]] = {}
        for row in result:
            current = by_path.get(str(row["path"]))
            if current is None or (row["hasMediaInfo"] and not current["hasMediaInfo"]):
                by_path[str(row["path"])] = row
        return list(by_path.values())

    @classmethod
    def has_media_info(cls, payload: dict[str, Any], media_source_id: str = "") -> bool:
        return bool(cls.media_info_summary(payload, media_source_id)["available"])

    @staticmethod
    def media_info_summary(payload: dict[str, Any], media_source_id: str = "") -> dict[str, Any]:
        sources = payload.get("MediaSources")
        matched_source = next(
            (
                value
                for value in sources or []
                if isinstance(value, dict)
                and (not media_source_id or str(value.get("Id") or "") == media_source_id)
            ),
            None,
        )
        source = matched_source or payload
        allow_item_fallback = not media_source_id or matched_source is None
        streams = (
            source.get("MediaStreams")
            or (payload.get("MediaStreams") if allow_item_fallback else [])
            or []
        )
        video = next(
            (
                value
                for value in streams
                if isinstance(value, dict) and str(value.get("Type") or "").casefold() == "video"
            ),
            {},
        )
        fallback = payload if allow_item_fallback else {}
        runtime_ticks = int(source.get("RunTimeTicks") or fallback.get("RunTimeTicks") or 0)
        size = int(source.get("Size") or fallback.get("Size") or 0)
        width = int(video.get("Width") or fallback.get("Width") or 0)
        height = int(video.get("Height") or fallback.get("Height") or 0)
        container = str(source.get("Container") or fallback.get("Container") or "")
        return {
            "available": bool(runtime_ticks > 0 and streams and container.casefold() != "strm"),
            "container": container,
            "videoCodec": str(video.get("Codec") or ""),
            "resolution": f"{width}x{height}" if width and height else "",
            "durationSeconds": round(runtime_ticks / 10_000_000, 1),
            "sizeBytes": size,
        }
