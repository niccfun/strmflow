from typing import Any

from fastapi import APIRouter, Depends, Request

from strmflow.api.deps import require_auth, services
from strmflow.api.routes.common import ok
from strmflow.core.errors import AppError
from strmflow.schemas.api import ItemTransferRequest, TransferCreateRequest

router = APIRouter(tags=["transfers"], dependencies=[Depends(require_auth)])


@router.get("/transfers/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    return ok({"providers": services(request).transfers.capabilities()})


@router.post("/transfers/preview")
async def preview(body: TransferCreateRequest, request: Request) -> dict[str, Any]:
    return ok(services(request).transfers.preview(body))


@router.post("/transfers", status_code=202)
async def create(body: TransferCreateRequest, request: Request) -> dict[str, Any]:
    job = await services(request).transfers.enqueue(body)
    return ok(job.model_dump(by_alias=True, mode="json"))


@router.post("/items/{item_id}/transfer", status_code=202)
async def create_for_item(
    item_id: str, body: ItemTransferRequest, request: Request
) -> dict[str, Any]:
    container = services(request)
    item = await container.media.get_item(item_id)
    if not item.get("baiduLink"):
        raise AppError(409, "该媒体配置未填写百度网盘链接")
    transfer = TransferCreateRequest(
        provider=body.provider,
        share_url=item["baiduLink"],
        destination=body.destination,
        extract_code=body.extract_code,
        metadata={"mediaItemId": item_id},
    )
    job = await container.transfers.enqueue(transfer)
    return ok(job.model_dump(by_alias=True, mode="json"))


@router.get("/transfers")
async def list_jobs(request: Request) -> dict[str, Any]:
    jobs = await services(request).transfers.list()
    return ok({"jobs": [job.model_dump(by_alias=True, mode="json") for job in jobs]})


@router.get("/transfers/{job_id}")
async def get(job_id: str, request: Request) -> dict[str, Any]:
    job = await services(request).transfers.get(job_id)
    return ok(job.model_dump(by_alias=True, mode="json"))
