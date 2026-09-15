from typing import Any

from fastapi import APIRouter, Depends, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.schemas.api import WecomWebhookConfigUpdate

router = APIRouter(tags=["notifications"], dependencies=[Depends(require_auth)])


@router.get("/notifications/wecom")
async def status(request: Request) -> dict[str, Any]:
    return ok(services(request).notifications.status())


@router.put("/notifications/wecom")
async def update(body: WecomWebhookConfigUpdate, request: Request) -> dict[str, Any]:
    return ok(await services(request).notifications.update_config(body))


@router.post("/notifications/wecom/test")
async def send_test(request: Request) -> dict[str, Any]:
    return ok(await services(request).notifications.send_test())
