from typing import Any

from fastapi import APIRouter, Depends, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.schemas.api import Emby302ConfigUpdate

router = APIRouter(
    prefix="/emby302",
    tags=["emby302"],
    dependencies=[Depends(require_auth)],
)


@router.get("")
async def get_emby302(request: Request) -> dict[str, Any]:
    return ok(services(request).emby302.snapshot())


@router.put("")
async def update_emby302(body: Emby302ConfigUpdate, request: Request) -> dict[str, Any]:
    return ok(await services(request).emby302.update(body.model_dump(by_alias=True)))


@router.post("/cache/clear")
async def clear_emby302_cache(request: Request) -> dict[str, Any]:
    gateway = services(request).emby302
    return ok({"cleared": gateway.clear_cache(), **gateway.snapshot()})
