import pytest

from strmflow.core.config import Settings
from strmflow.services.media import MediaService
from strmflow.utils.episodes import (
    media_quality_label,
    media_quality_rank,
    select_preferred_episodes,
)


class VirtualStrmOpenList:
    def __init__(self) -> None:
        self.copies: list[tuple[str, str, list[str]]] = []
        self.directories: list[str] = []
        self.renames: list[tuple[str, list[dict[str, str]]]] = []

    async def mkdir(self, path: str) -> None:
        self.directories.append(path)

    async def copy(self, source_dir: str, target_dir: str, names: list[str]) -> None:
        self.copies.append((source_dir, target_dir, names))

    async def get_file_info(self, path: str) -> dict[str, int]:
        assert path == "/target/Season 02/S01E01.strm"
        return {"size": 128}

    async def batch_rename(self, directory: str, changes: list[dict[str, str]]) -> None:
        self.renames.append((directory, changes))


def test_configured_season_is_used_when_filename_has_only_episode() -> None:
    context = {"mediaType": "tv", "name": "示例剧", "season": 2}
    assert MediaService._normalized_target_name("E03.strm", context) == (
        "Season 02/示例剧 - S02E03.strm"
    )
    assert MediaService._normalized_target_name("第4集.strm", context) == (
        "Season 02/示例剧 - S02E04.strm"
    )


def test_duplicate_episode_prefers_canonical_high_quality_file() -> None:
    files = [
        "S01E06 1080p WEB-DL.strm",
        "S01E06 4K WEB-DL.strm",
        "S01E06 4K WEB-DL(1).strm",
    ]
    preferred, duplicates = select_preferred_episodes(files, path=lambda value: value)
    assert preferred == ["S01E06 4K WEB-DL.strm"]
    assert set(duplicates) == {
        "S01E06 1080p WEB-DL.strm",
        "S01E06 4K WEB-DL(1).strm",
    }


def test_compact_quality_tags_prefer_4k_hdr_60fps() -> None:
    files = [
        "S01E04 4K60FPS-GyWEB.strm",
        "S01E04 4KHDR60FPS-GyWEB.strm",
        "S01E04 1080P HDR60FPS.strm",
    ]
    preferred, duplicates = select_preferred_episodes(files, path=lambda value: value)
    assert preferred == ["S01E04 4KHDR60FPS-GyWEB.strm"]
    assert len(duplicates) == 2
    assert media_quality_rank("4KHDR60FPS.mp4") > media_quality_rank("4KHDR30FPS.mp4")
    assert media_quality_rank("4KHDR30FPS.mp4") > media_quality_rank("4K60FPS.mp4")
    assert media_quality_label("S01E04.4KHDR60FPS.WEB-DL.HEVC.strm") == ("4K HDR 60FPS WEB-DL HEVC")


def test_initial_publish_is_canonical_and_later_upgrade_becomes_emby_version() -> None:
    service = MediaService(Settings(), None, None, None, None)  # type: ignore[arg-type]
    context = {
        "mediaType": "tv",
        "name": "示例剧 (2026)",
        "season": 1,
        "syncedFiles": ["S01E01.1080P.WEB-DL.strm"],
    }
    canonical = "Season 01/示例剧 (2026) - S01E01.strm"
    plan = service._build_plan(["S01E01.4KHDR60FPS.WEB-DL.HEVC.strm"], context, None)
    upgraded, upgrades = service._apply_version_targets(plan, context, {canonical})

    assert upgraded[0]["targetRel"] == (
        "Season 01/示例剧 (2026) - S01E01 - 4K HDR 60FPS WEB-DL HEVC.strm"
    )
    assert upgrades == [
        {
            "sourceRel": "S01E01.4KHDR60FPS.WEB-DL.HEVC.strm",
            "targetRel": upgraded[0]["targetRel"],
            "from": "1080P WEB-DL",
            "to": "4K HDR 60FPS WEB-DL HEVC",
        }
    ]
    confirmed, confirmed_upgrades = service._apply_version_targets(upgraded, context, {canonical})
    assert confirmed == upgraded
    assert confirmed_upgrades == upgrades


def test_named_emby_versions_are_retained_but_legacy_numbered_collisions_are_removed() -> None:
    service = MediaService(Settings(), None, None, None, None)  # type: ignore[arg-type]
    files = [
        "Season 01/示例剧 - S01E01.strm",
        "Season 01/示例剧 - S01E01 - 4K HDR.strm",
        "Season 01/示例剧 - S01E01 - 2.strm",
        "Season 01/S01E01 4KHDR-GROUP.strm",
    ]
    assert service._legacy_target_duplicates(files, {"season": 1, "name": "示例剧"}) == [
        files[2],
        files[3],
    ]


def test_synced_inventory_keeps_highest_known_quality_without_duplicate_paths() -> None:
    service = MediaService(Settings(), None, None, None, None)  # type: ignore[arg-type]
    current = ["S01E01.4K.HDR.strm", "S01E02.1080P.strm"]
    merged = service._merge_synced_inventory(
        current,
        {
            "mediaType": "tv",
            "season": 1,
            "syncedFiles": ["S01E01.1080P.strm", "S01E02.1080P.strm"],
        },
    )
    assert merged == ["S01E01.4K.HDR.strm", "S01E02.1080P.strm"]


def test_generated_numbered_duplicate_never_replaces_canonical_target() -> None:
    files = [
        "Season 01/示例剧 - S01E04 - 2.strm",
        "Season 01/示例剧 - S01E04.strm",
    ]
    preferred, duplicates = select_preferred_episodes(files, path=lambda value: value)
    assert preferred == ["Season 01/示例剧 - S01E04.strm"]
    assert duplicates == ["Season 01/示例剧 - S01E04 - 2.strm"]


def test_episode_count_deduplicates_same_episode_suffixes() -> None:
    service = MediaService(Settings(), None, None, None, None)  # type: ignore[arg-type]
    assert (
        service._episode_count(
            ["S01E06 4K.strm", "S01E06 4K(1).strm", "S01E07 4K.strm"],
            {"mediaType": "tv", "season": 1},
        )
        == 2
    )


def test_new_episode_identities_exclude_quality_upgrades() -> None:
    context = {"mediaType": "tv", "name": "示例剧", "season": 1}
    current = [
        "S01E12.4KHDR.strm",
        "S01E13.4KHDR.strm",
        "S01E13.1080P.strm",
    ]
    previous = ["S01E12.1080P.strm"]

    assert MediaService._new_episode_identities(current, previous, context) == [(1, 13)]
    assert MediaService._new_episode_count(current, previous, context) == 1


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
async def test_virtual_strm_uses_openlist_copy_to_materialize_manifest() -> None:
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
    assert openlist.copies == [("/source", "/target/Season 02", ["S01E01.strm"])]
    assert openlist.directories == ["/target/Season 02"]
    assert openlist.renames == [
        (
            "/target/Season 02",
            [
                {
                    "src_name": "S01E01.strm",
                    "new_name": "示例剧 - S02E01.strm",
                }
            ],
        )
    ]
    assert renamed == [{"from": "S01E01.strm", "to": "Season 02/示例剧 - S02E01.strm"}]
