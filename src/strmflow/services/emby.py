from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx

from strmflow.core.config import Settings
from strmflow.core.errors import AppError, UpstreamError
from strmflow.utils.paths import normalize_virtual_path

EMBY_SCAN_PAGE_SIZE = 500


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

    async def list_media_libraries(self, *, timeout: float = 10) -> list[dict[str, Any]]:
        """List Emby virtual folders for user-selectable episode image coverage."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(409, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        try:
            response = await self.http.get(
                "/emby/Library/VirtualFolders",
                headers={
                    "X-Emby-Token": self.settings.emby_api_key,
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise UpstreamError("Emby 媒体库列表读取超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Emby 媒体库列表读取失败") from exc
        except ValueError as exc:
            raise UpstreamError("Emby 媒体库列表返回了无效数据") from exc
        if not isinstance(payload, list):
            raise UpstreamError("Emby 媒体库列表返回了无效数据")
        libraries: list[dict[str, Any]] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            library_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Guid") or "").strip()
            if not library_id:
                continue
            locations = [
                normalize_virtual_path(str(location))
                for location in raw.get("Locations") or []
                if str(location).strip().startswith("/")
            ]
            libraries.append(
                {
                    "id": library_id,
                    "name": str(raw.get("Name") or library_id),
                    "collectionType": str(raw.get("CollectionType") or ""),
                    "locations": list(dict.fromkeys(locations)),
                }
            )
        return libraries

    @staticmethod
    def media_library_for_path(path: str, libraries: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Match a media path to the most specific Emby virtual folder location."""
        normalized_path = normalize_virtual_path(path)
        matches: list[tuple[int, dict[str, Any]]] = []
        for library in libraries:
            for location in library.get("locations") or []:
                normalized_location = normalize_virtual_path(str(location))
                if normalized_path == normalized_location or normalized_path.startswith(
                    normalized_location.rstrip("/") + "/"
                ):
                    matches.append((len(normalized_location), library))
        return max(matches, key=lambda value: value[0])[1] if matches else None

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
                    "Fields": "Path,MediaSources,MediaStreams,ImageTags",
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

    async def get_item(self, item_id: str, *, timeout: float = 10) -> dict[str, Any] | None:
        """Read one Emby item, including its own image tags."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            return None
        try:
            response = await self.http.get(
                f"/emby/Items/{item_id}",
                params={"Fields": "Path,MediaSources,MediaStreams,ImageTags"},
                headers={
                    "X-Emby-Token": self.settings.emby_api_key,
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise UpstreamError("Emby 剧集图片状态检查超时", status_code=504) from exc
        except httpx.HTTPStatusError as exc:
            raise UpstreamError(
                f"Emby 剧集图片状态检查失败：HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Emby 剧集图片状态检查失败") from exc
        except ValueError as exc:
            raise UpstreamError("Emby 剧集信息接口返回了无效数据") from exc
        return payload if isinstance(payload, dict) else None

    async def upload_primary_image(
        self,
        item_id: str,
        image: bytes,
        *,
        content_type: str = "image/jpeg",
        timeout: float = 30,
    ) -> None:
        """Upload an item primary image through Emby's authenticated image API."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(409, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        if not image:
            raise ValueError("待上传的剧集图片为空")
        try:
            response = await self.http.post(
                f"/emby/Items/{item_id}/Images/Primary",
                content=base64.b64encode(image),
                headers={
                    "X-Emby-Token": self.settings.emby_api_key,
                    "Content-Type": content_type,
                    "Accept": "application/json",
                },
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise UpstreamError("上传 Emby 剧集图片超时", status_code=504) from exc
        except httpx.HTTPStatusError as exc:
            raise UpstreamError(f"上传 Emby 剧集图片失败：HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("上传 Emby 剧集图片失败") from exc

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
        """List STRM sources using bounded concurrent Emby-only page requests."""
        if not self.settings.emby_url or not self.settings.emby_api_key:
            raise AppError(409, "尚未配置 EMBY_URL 或 EMBY_API_KEY")
        normalized_root = normalize_virtual_path(root)
        prefix = normalized_root.rstrip("/") + "/"
        first_items, total = await self._list_media_page(0, EMBY_SCAN_PAGE_SIZE, timeout)
        all_items = list(first_items)
        if first_items and len(first_items) < total:
            # Emby may cap the requested page size. Use the number it actually
            # returned as the stride so no item is skipped.
            page_size = len(first_items)
            starts = list(range(page_size, total, page_size))
            semaphore = asyncio.Semaphore(self.settings.media_probe_scan_concurrency)

            async def fetch_page(start: int) -> list[dict[str, Any]]:
                async with semaphore:
                    items, _ = await self._list_media_page(start, page_size, timeout)
                    return items

            tasks = [
                asyncio.create_task(fetch_page(start), name=f"emby-missing-scan-page-{start}")
                for start in starts
            ]
            try:
                pages = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            for items in pages:
                all_items.extend(items)

        result: list[dict[str, Any]] = []
        for item in all_items:
            if not item.get("Id"):
                continue
            # After Emby has successfully probed a STRM, MediaSources[].Path is
            # replaced by the resolved HTTP media URL. Item.Path remains stable.
            item_path = str(item.get("Path") or "")
            normalized_item_path = (
                normalize_virtual_path(item_path) if item_path.casefold().endswith(".strm") else ""
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
                        "itemType": str(item.get("Type") or ""),
                        "hasPrimaryImage": self.has_primary_image(item),
                    }
                )
        # A grouped Emby item can expose more than one source for the same STRM
        # item.  It is sufficient for one of them to contain native media info;
        # retain that source rather than letting response order decide the result.
        by_path: dict[str, dict[str, Any]] = {}
        for row in result:
            current = by_path.get(str(row["path"]))
            if current is None or (row["hasMediaInfo"] and not current["hasMediaInfo"]):
                by_path[str(row["path"])] = row
        return list(by_path.values())

    async def _list_media_page(
        self, start: int, limit: int, timeout: float
    ) -> tuple[list[dict[str, Any]], int]:
        try:
            response = await self.http.get(
                "/emby/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Episode,Movie",
                    "Fields": "Path,MediaSources,MediaStreams,ImageTags",
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
            raise UpstreamError("Emby 媒体增强扫描超时", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("Emby 媒体增强扫描请求失败") from exc
        except ValueError as exc:
            raise UpstreamError("Emby 媒体列表返回了无效数据") from exc
        items = payload.get("Items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise UpstreamError("Emby 媒体列表返回了无效数据")
        try:
            total = max(len(items) + start, int(payload.get("TotalRecordCount") or 0))
        except (TypeError, ValueError) as exc:
            raise UpstreamError("Emby 媒体列表返回了无效总数") from exc
        return items, total

    @classmethod
    def has_media_info(cls, payload: dict[str, Any], media_source_id: str = "") -> bool:
        return bool(cls.media_info_summary(payload, media_source_id)["available"])

    @staticmethod
    def has_primary_image(payload: dict[str, Any]) -> bool:
        """Return whether the item owns a primary image (not an inherited parent image)."""
        image_tags = payload.get("ImageTags")
        return bool(isinstance(image_tags, dict) and image_tags.get("Primary"))

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
