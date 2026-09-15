import json

import httpx

from strmflow.core.config import Settings
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
