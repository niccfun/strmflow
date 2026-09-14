import pytest

from strmflow.core.config import Settings
from strmflow.services.media import MediaService


class VirtualStrmOpenList:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.copies: list[tuple[str, str, list[str]]] = []
        self.directories: list[str] = []

    async def mkdir(self, path: str) -> None:
        self.directories.append(path)

    async def get_file_info(self, path: str) -> dict[str, str]:
        return {
            "provider": "Strm",
            "raw_url": "https://openlist.example/p/source/video.strm?sign=TOKEN",
        }

    async def write_text(self, path: str, text: str) -> None:
        self.writes.append((path, text))

    async def copy(self, source_dir: str, target_dir: str, names: list[str]) -> None:
        self.copies.append((source_dir, target_dir, names))

    async def batch_rename(self, directory: str, changes: list[dict[str, str]]) -> None:
        raise AssertionError("虚拟 STRM 文件不应调用批量重命名")


def test_configured_season_is_used_when_filename_has_only_episode() -> None:
    context = {"mediaType": "tv", "name": "示例剧", "season": 2}
    assert MediaService._normalized_target_name("E03.strm", context) == (
        "Season 02/示例剧 - S02E03.strm"
    )
    assert MediaService._normalized_target_name("第4集.strm", context) == (
        "Season 02/示例剧 - S02E04.strm"
    )


def test_configured_season_takes_priority_over_filename_season() -> None:
    context = {"mediaType": "tv", "name": "示例剧", "season": 2}
    assert MediaService._normalized_target_name("S03E05.strm", context) == (
        "Season 02/示例剧 - S02E05.strm"
    )
    assert MediaService._normalized_target_name("Season 7 Episode 8.strm", context) == (
        "Season 02/示例剧 - S02E08.strm"
    )


def test_source_season_folder_is_flattened_into_emby_season_directory() -> None:
    context = {"mediaType": "tv", "name": "示例剧 (2026)", "season": 2}
    assert MediaService._normalized_target_name("Season 1/S01E03.strm", context) == (
        "Season 02/示例剧 (2026) - S02E03.strm"
    )


def test_multiple_seasons_are_detected_from_directories_and_filenames() -> None:
    service = MediaService(
        Settings(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    context = {"mediaType": "tv", "name": "示例剧 (2026)", "season": 1}

    plan = service._build_plan(
        [
            "Season 1/E01.strm",
            "第2季/第1集.strm",
            "S03E02.strm",
        ],
        context,
        None,
    )

    assert plan == [
        {
            "sourceRel": "Season 1/E01.strm",
            "targetRel": "Season 01/示例剧 (2026) - S01E01.strm",
        },
        {
            "sourceRel": "第2季/第1集.strm",
            "targetRel": "Season 02/示例剧 (2026) - S02E01.strm",
        },
        {
            "sourceRel": "S03E02.strm",
            "targetRel": "Season 03/示例剧 (2026) - S03E02.strm",
        },
    ]


def test_single_season_still_uses_user_configured_season() -> None:
    service = MediaService(
        Settings(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    plan = service._build_plan(
        ["Season 7/E01.strm", "Season 7/E02.strm"],
        {"mediaType": "tv", "name": "示例剧", "season": 2},
        None,
    )

    assert [entry["targetRel"] for entry in plan] == [
        "Season 02/示例剧 - S02E01.strm",
        "Season 02/示例剧 - S02E02.strm",
    ]


def test_multi_season_supporting_files_use_canonical_season_directories() -> None:
    context = {
        "mediaType": "tv",
        "name": "示例剧",
        "season": 1,
        "autoMultiSeason": True,
    }
    assert MediaService._normalized_target_name("第2季/poster.jpg", context) == (
        "Season 02/poster.jpg"
    )


def test_tv_target_uses_emby_series_root() -> None:
    service = MediaService(
        Settings(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        type("PathConfig", (), {"emby_strm_root": "/emby"})(),  # type: ignore[arg-type]
    )

    target = service._build_target(
        {
            "title": "示例剧",
            "year": "2026",
            "mediaType": "tv",
            "category": "国产剧",
            "season": 2,
        }
    )

    assert target["targetDir"] == "/emby/tv/国产剧/示例剧 (2026)"


@pytest.mark.anyio
async def test_virtual_strm_is_materialized_as_url_text_instead_of_server_copy() -> None:
    openlist = VirtualStrmOpenList()
    service = MediaService(
        Settings(),
        openlist,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    copied, renamed = await service._copy_plan(
        "/source",
        "/target",
        [
            {
                "sourceRel": "S01E01.strm",
                "targetRel": "Season 02/示例剧 - S02E01.strm",
            }
        ],
    )

    assert copied == 1
    assert openlist.copies == []
    assert openlist.directories == ["/target/Season 02"]
    assert openlist.writes == [
        (
            "/target/Season 02/示例剧 - S02E01.strm",
            "https://openlist.example/p/source/video.strm?sign=TOKEN",
        )
    ]
    assert renamed == [{"from": "S01E01.strm", "to": "Season 02/示例剧 - S02E01.strm"}]
