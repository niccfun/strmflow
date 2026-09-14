from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.schemas.api import (
    BdpanAutomationConfigUpdate,
    BdpanLoginCompleteRequest,
    BdpanLoginStartRequest,
)

router = APIRouter(tags=["bdpan"], dependencies=[Depends(require_auth)])


@router.get("/bdpan")
async def status(request: Request, refresh: int = Query(default=0)) -> dict[str, Any]:
    return ok(await services(request).bdpan.status(refresh=refresh == 1))


@router.put("/bdpan")
async def update(body: BdpanAutomationConfigUpdate, request: Request) -> dict[str, Any]:
    return ok(await services(request).bdpan.update_config(body))


@router.post("/bdpan/check", status_code=202)
async def check_all(request: Request) -> dict[str, Any]:
    return ok(await services(request).bdpan.trigger_check())


@router.post("/bdpan/items/{item_id}/check", status_code=202)
async def check_item(item_id: str, request: Request) -> dict[str, Any]:
    return ok(await services(request).bdpan.trigger_check(item_id))


@router.post("/bdpan/login/start")
async def login_start(body: BdpanLoginStartRequest, request: Request) -> dict[str, Any]:
    return ok(await services(request).bdpan.start_login(body.accepted))


@router.post("/bdpan/login/complete")
async def login_complete(body: BdpanLoginCompleteRequest, request: Request) -> dict[str, Any]:
    return ok(await services(request).bdpan.complete_login(body.code))
