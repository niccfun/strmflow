from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.core.errors import UpstreamError
from strmflow.schemas.api import MediaProbeConfigUpdate, PathConfigUpdate

router = APIRouter(tags=["system"], dependencies=[Depends(require_auth)])


@router.get("/health")
async def health() -> dict[str, Any]:
    return ok({"ok": True})


@router.get("/status/overview")
async def status_overview(request: Request, refresh: int = Query(default=0)) -> dict[str, Any]:
    return ok(await services(request).system_status.snapshot(refresh=refresh == 1))


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    container = services(request)
    settings = request.app.state.settings
    path_config = container.path_config
    return ok(
        {
            "listRoot": path_config.list_root or "未设置只读源 STRM 根目录",
            "scanRoot": path_config.list_root or "未设置只读源 STRM 根目录",
            "scanLimit": settings.scan_limit,
            "embyStrmRoot": path_config.emby_strm_root,
            "pathSettings": path_config.as_dict(),
            "embyConfigured": bool(settings.emby_url and settings.emby_api_key),
            "openListWebUrl": settings.openlist_web_url or settings.openlist_url,
            "openListInternalUrl": settings.openlist_url,
            "openListExternalUrl": settings.openlist_web_url,
            "embyWebUrl": settings.emby_web_url or settings.emby_url,
            "embyInternalUrl": settings.emby_url,
            "embyExternalUrl": settings.emby_web_url,
            "strmConfigured": bool(path_config.list_root),
            "transferProviders": container.transfers.capabilities(),
        }
    )


@router.get("/logs")
async def runtime_logs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 300,
    category: Annotated[str, Query(max_length=32)] = "",
) -> dict[str, Any]:
    store = request.app.state.runtime_logs
    return ok(
        {
            "items": store.list(limit=limit, category=category.strip()),
            "categories": store.categories(),
        }
    )


@router.delete("/logs")
async def clear_runtime_logs(request: Request) -> dict[str, Any]:
    return ok({"cleared": request.app.state.runtime_logs.clear()})


@router.put("/settings/paths")
async def update_paths(body: PathConfigUpdate, request: Request) -> dict[str, Any]:
    container = services(request)
    previous_root = container.path_config.emby_strm_root
    try:
        await container.storage.assert_publish_target_isolated(body.list_root, body.emby_strm_root)
    except UpstreamError:
        # Direct overlap was already checked. Let users repair path settings
        # while OpenList is temporarily unavailable; publish validates again.
        pass
    value = await container.path_config.update(body.model_dump(by_alias=True))
    updated_items = 0
    if value["embyStrmRoot"] != previous_root:
        updated_items = await container.media.rebase_target_root(value["embyStrmRoot"])
    request.app.state.runtime_logs.add(
        category="settings",
        level="success",
        message="STRM 路径设置已保存",
        sourceRoot=value["listRoot"],
        targetRoot=value["embyStrmRoot"],
        updatedItemCount=updated_items,
    )
    return ok({**value, "updatedItems": updated_items})


@router.get("/media-probe")
async def media_probe_status(request: Request) -> dict[str, Any]:
    return ok(await services(request).media_probe.status())


@router.put("/media-probe")
async def update_media_probe(body: MediaProbeConfigUpdate, request: Request) -> dict[str, Any]:
    return ok(await services(request).media_probe.update_config(body))


@router.post("/media-probe/scan", status_code=202)
async def scan_missing_media_info(request: Request) -> dict[str, Any]:
    return ok(await services(request).media_probe.trigger_scan())
