import json

import httpx
import pytest

from strmflow.core.config import Settings
from strmflow.core.errors import UpstreamError
from strmflow.services.openlist import OpenListClient


async def test_batch_rename_retries_individually_when_openlist_rejects_batch() -> None:
    rename_calls: list[list[dict[str, str]]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/api/fs/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "content": [
                            {"name": "source-1.strm", "is_dir": False},
                            {"name": "source-2.strm", "is_dir": False},
                            {"name": "target-1.strm", "is_dir": False},
                            {"name": "target-2.strm", "is_dir": False},
                        ]
                    },
                },
            )
        changes = payload["rename_objects"]
        rename_calls.append(changes)
        if len(changes) > 1:
            return httpx.Response(200, json={"code": 500, "message": "object not found"})
        return httpx.Response(200, json={"code": 200, "data": None})

    transport = httpx.MockTransport(upstream)
    settings = Settings(openlist_url="http://openlist.test", openlist_token="token")
    changes = [
        {"src_name": "source-1.strm", "new_name": "target-1.strm"},
        {"src_name": "source-2.strm", "new_name": "target-2.strm"},
    ]
    async with httpx.AsyncClient(base_url=settings.openlist_url, transport=transport) as http:
        await OpenListClient(settings, http).batch_rename("/library", changes)

    assert rename_calls == [changes, [changes[0]], [changes[1]]]


async def test_batch_rename_accepts_destination_from_partially_applied_batch() -> None:
    rename_calls: list[list[dict[str, str]]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/api/fs/list":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "content": [
                            {"name": "target-1.strm", "is_dir": False},
                            {"name": "source-2.strm", "is_dir": False},
                        ]
                    },
                },
            )
        changes = payload["rename_objects"]
        rename_calls.append(changes)
        if len(changes) > 1:
            return httpx.Response(200, json={"code": 500, "message": "object not found"})
        return httpx.Response(200, json={"code": 200, "data": None})

    transport = httpx.MockTransport(upstream)
    settings = Settings(openlist_url="http://openlist.test", openlist_token="token")
    changes = [
        {"src_name": "source-1.strm", "new_name": "target-1.strm"},
        {"src_name": "source-2.strm", "new_name": "target-2.strm"},
    ]
    async with httpx.AsyncClient(base_url=settings.openlist_url, transport=transport) as http:
        await OpenListClient(settings, http).batch_rename("/library", changes)

    assert rename_calls == [changes, [changes[1]]]


async def test_list_dir_reads_every_openlist_page() -> None:
    requested_pages: list[tuple[int, bool]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        page = int(payload["page"])
        requested_pages.append((page, bool(payload["refresh"])))
        count = 1_000 if page == 1 else 205
        offset = (page - 1) * 1_000
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "content": [
                        {"name": f"item-{offset + index}", "is_dir": False}
                        for index in range(count)
                    ],
                    "total": 1_205,
                },
            },
        )

    transport = httpx.MockTransport(upstream)
    settings = Settings(openlist_url="http://openlist.test", openlist_token="token")
    async with httpx.AsyncClient(base_url=settings.openlist_url, transport=transport) as http:
        items = await OpenListClient(settings, http).list_dir("/library", refresh=True)

    assert len(items) == 1_205
    assert requested_pages == [(1, True), (2, False)]


async def test_mkdir_does_not_hide_unrelated_bad_request() -> None:
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "message": "permission denied"})

    transport = httpx.MockTransport(upstream)
    settings = Settings(openlist_url="http://openlist.test", openlist_token="token")
    async with httpx.AsyncClient(base_url=settings.openlist_url, transport=transport) as http:
        with pytest.raises(UpstreamError, match="permission denied"):
            await OpenListClient(settings, http).mkdir("/library")


async def test_mkdir_accepts_existing_directory_conflict() -> None:
    async def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "message": "object already exists"})

    transport = httpx.MockTransport(upstream)
    settings = Settings(openlist_url="http://openlist.test", openlist_token="token")
    async with httpx.AsyncClient(base_url=settings.openlist_url, transport=transport) as http:
        await OpenListClient(settings, http).mkdir("/library")
