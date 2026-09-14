from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.core.errors import AppError
from strmflow.schemas.api import IdRequest, MediaItemInput, PublishRequest, ScanRequest
from strmflow.utils.paths import normalize_virtual_path, validate_folder_name

router = APIRouter(tags=["media"], dependencies=[Depends(require_auth)])


@router.get("/media")
async def list_media(request: Request, refresh: int = Query(default=0)) -> dict[str, Any]:
    folders = await services(request).storage.list_media_folders(refresh == 1)
    return ok({"folders": folders, "count": len(folders)})


@router.get("/media/options")
async def media_options(
    request: Request,
    type_dir: str | None = Query(default=None, alias="typeDir"),
    category: str | None = Query(default=None),
    refresh: int = Query(default=0),
) -> dict[str, Any]:
    return ok(
        await services(request).storage.list_media_options(
            type_dir,
            category,
            refresh=refresh == 1,
        )
    )


@router.get("/items")
async def list_items(request: Request) -> dict[str, Any]:
    result = await services(request).media.list_items()
    return ok({"items": result, "count": len(result)})


@router.post("/items/save")
async def save_item(body: MediaItemInput, request: Request) -> dict[str, Any]:
    container = services(request)
    item = await container.media.save_item(body)
    await container.bdpan.media_updated(item)
    return ok({"item": item})


@router.post("/items/delete")
async def delete_item(body: IdRequest, request: Request) -> dict[str, Any]:
    container = services(request)
    await container.media.delete_item(body.id)
    await container.bdpan.forget_item(body.id)
    return ok({"deleted": True})


@router.post("/items/scan/start")
async def item_scan(body: IdRequest, request: Request) -> dict[str, Any]:
    container = services(request)
    item = await container.media.get_item(body.id)
    await container.openlist.request(
        "POST",
        "/api/admin/scan/start",
        {"path": item["sourcePath"], "limit": request.app.state.settings.scan_limit},
    )
    return ok({"started": True, "name": item["name"], "scanPath": item["sourcePath"]})


@router.post("/scan/start")
async def scan(body: ScanRequest, request: Request) -> dict[str, Any]:
    container = services(request)
    name = validate_folder_name(body.name)
    folders = await container.storage.list_media_folders(False)
    folder = next(
        (
            item
            for item in folders
            if item["name"] == name
            and (
                body.scan_path is None or item["scanPath"] == normalize_virtual_path(body.scan_path)
            )
            and (
                body.media_path is None
                or item["mediaPath"] == normalize_virtual_path(body.media_path)
            )
        ),
        None,
    )
    if not folder:
        raise AppError(404, "媒体目录不存在或已被移除")
    await container.openlist.request(
        "POST",
        "/api/admin/scan/start",
        {"path": folder["scanPath"], "limit": request.app.state.settings.scan_limit},
    )
    return ok({"started": True, "name": name, "scanPath": folder["scanPath"]})


@router.get("/scan/progress")
async def scan_progress(request: Request) -> dict[str, Any]:
    data = await services(request).openlist.request("GET", "/api/admin/scan/progress")
    return ok(
        {
            "objectCount": int((data or {}).get("obj_count", 0)),
            "done": bool((data or {}).get("is_done")),
        }
    )


@router.post("/emby/refresh")
async def refresh_emby(request: Request) -> dict[str, Any]:
    await services(request).emby.refresh_library()
    return ok({"refreshed": True})


@router.post("/emby/publish/preview")
async def preview_publish(body: PublishRequest, request: Request) -> dict[str, Any]:
    return ok(await services(request).media.preview_publish(body))


@router.post("/emby/publish")
async def publish(body: PublishRequest, request: Request) -> dict[str, Any]:
    return ok(await services(request).media.publish(body))
