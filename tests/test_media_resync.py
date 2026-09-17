from __future__ import annotations

from typing import Any

import pytest

from strmflow.core.config import Settings
from strmflow.schemas.api import PublishRequest
from strmflow.services.media import STRM_MANIFEST_VERSION, MediaService


class FakeOpenList:
    def __init__(self) -> None:
        self.copies: list[tuple[str, str, list[str]]] = []
        self.removals: list[tuple[str, list[str]]] = []
        self.renames: list[tuple[str, list[dict[str, str]]]] = []

    async def list_dir(self, path: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        assert refresh is True
        if path == "/source/电视剧/欧美剧/Show (2026)":
            return [{"name": "Show.S01E01.strm", "is_dir": False, "size": 128}]
        if path == "/target/电视剧/欧美剧/Show (2026)":
            return [{"name": "Season 01", "is_dir": True, "size": 0}]
        if path == "/target/电视剧/欧美剧/Show (2026)/Season 01":
            return [
                {
                    "name": "Show (2026) - S01E01.strm",
                    "is_dir": False,
                    "size": 128,
                }
            ]
        return []

    async def mkdir(self, _path: str) -> None:
        return None

    async def copy(self, source: str, target: str, names: list[str]) -> None:
        self.copies.append((source, target, names))

    async def get_file_info(self, _path: str) -> dict[str, Any]:
        return {"size": 128}

    async def remove(self, directory: str, names: list[str]) -> None:
        self.removals.append((directory, names))

    async def batch_rename(self, directory: str, changes: list[dict[str, str]]) -> None:
        self.renames.append((directory, changes))


class FakeStorage:
    async def assert_publish_target_isolated(self, _source: str, _target: str) -> None:
        return None


class FakeRepository:
    def __init__(self) -> None:
        self.item = {
            "id": "m1",
            "name": "Show (2026)",
            "title": "Show",
            "year": "2026",
            "category": "欧美剧",
            "mediaType": "tv",
            "status": "ongoing",
            "season": 1,
            "totalEpisodes": "",
            "sourcePath": "/source/电视剧/欧美剧/Show (2026)",
            "generatedPath": "/source/电视剧/欧美剧/Show (2026)",
            "targetDir": "/target/电视剧/欧美剧/Show (2026)",
            "syncedFiles": ["Show.S01E01.strm"],
            "manifestVersion": STRM_MANIFEST_VERSION,
        }

    async def get(self, item_id: str) -> dict[str, Any] | None:
        return dict(self.item) if item_id == "m1" else None

    async def update(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        assert item_id == "m1"
        self.item.update(patch)
        return dict(self.item)


class FakePathConfig:
    list_root = "/source"
    emby_strm_root = "/target"


@pytest.mark.asyncio
async def test_resync_previews_and_replaces_existing_strm() -> None:
    openlist = FakeOpenList()
    service = MediaService(
        Settings(),
        openlist,  # type: ignore[arg-type]
        FakeStorage(),  # type: ignore[arg-type]
        FakeRepository(),  # type: ignore[arg-type]
        FakePathConfig(),  # type: ignore[arg-type]
    )

    regular = await service.preview_publish(PublishRequest(id="m1"))
    assert regular["plan"] == []
    assert regular["replacementCount"] == 0

    request = PublishRequest(
        id="m1",
        replaceExisting=True,
        renamePlan=[
            {
                "sourceRel": "Show.S01E01.strm",
                "targetRel": "Season 01/Show (2026) - S01E01.strm",
            }
        ],
    )
    preview = await service.preview_publish(request)
    assert preview["replaceExisting"] is True
    assert preview["replacementCount"] == 1
    assert preview["pendingFiles"] == 1
    assert preview["plan"] == [
        {
            "sourceRel": "Show.S01E01.strm",
            "targetRel": "Season 01/Show (2026) - S01E01.strm",
            "changed": True,
        }
    ]

    result = await service.publish(request)

    assert result["copied"] == 1
    assert result["replacedStrmFiles"] == 1
    assert openlist.copies == [
        (
            "/source/电视剧/欧美剧/Show (2026)",
            "/target/电视剧/欧美剧/Show (2026)/Season 01",
            ["Show.S01E01.strm"],
        )
    ]
    assert openlist.removals == [
        (
            "/target/电视剧/欧美剧/Show (2026)/Season 01",
            ["Show (2026) - S01E01.strm"],
        )
    ]
    assert openlist.renames == [
        (
            "/target/电视剧/欧美剧/Show (2026)/Season 01",
            [
                {
                    "src_name": "Show.S01E01.strm",
                    "new_name": "Show (2026) - S01E01.strm",
                }
            ],
        )
    ]
