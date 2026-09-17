from typing import Any

from fastapi import APIRouter, Depends, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.schemas.api import (
    TelegramConfigUpdate,
    TelegramLoginCompleteRequest,
    TelegramLoginStartRequest,
)

router = APIRouter(tags=["telegram"], dependencies=[Depends(require_auth)])


@router.get("/telegram")
async def status(request: Request) -> dict[str, Any]:
    return ok(await services(request).telegram.status())


@router.put("/telegram")
async def update(body: TelegramConfigUpdate, request: Request) -> dict[str, Any]:
    return ok(await services(request).telegram.update_config(body))


@router.post("/telegram/login/start")
async def login_start(body: TelegramLoginStartRequest, request: Request) -> dict[str, Any]:
    return ok(await services(request).telegram.start_login(body.phone))


@router.post("/telegram/login/complete")
async def login_complete(body: TelegramLoginCompleteRequest, request: Request) -> dict[str, Any]:
    return ok(await services(request).telegram.complete_login(body.code, body.password))


@router.post("/telegram/logout")
async def logout(request: Request) -> dict[str, Any]:
    return ok(await services(request).telegram.logout())
