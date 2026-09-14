import pytest

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.domain.models import Storage, StorageConfig
from strmflow.services.storage import StorageService


class DummyOpenList:
    pass


class DiscoveryOpenList:
    def __init__(self) -> None:
        self.listed_paths: list[str] = []

    async def request(self, method: str, path: str) -> dict[str, object]:
        assert method == "GET"
        assert path.startswith("/api/admin/storage/list")
        return {
            "content": [
                {
                    "driver": "Strm",
                    "mount_path": "/strm",
                    "addition": {},
                }
            ]
        }

    async def list_dir(self, path: str, refresh: bool = False) -> list[dict[str, object]]:
        assert refresh is False
        self.listed_paths.append(path)
        return {
            "/strm": [{"name": "tv", "is_dir": True}],
            "/strm/tv": [{"name": "国产剧", "is_dir": True}],
            "/strm/tv/国产剧": [{"name": "百花杀 (2026)", "is_dir": True}],
            "/strm/tv/国产剧/百花杀 (2026)": [{"name": "百花杀.S01E01.strm", "is_dir": False}],
        }.get(path, [])


class EmptyContentOpenList(DiscoveryOpenList):
    async def list_dir(self, path: str, refresh: bool = False) -> None:
        return None


def test_resolve_direct_strm_path() -> None:
    service = StorageService(Settings(), DummyOpenList())  # type: ignore[arg-type]
    storage = Storage(None, "Strm", "/temp_strm")
    storage.save_virtual_root = "/temp_strm"
    config = StorageConfig(strm_storages=[storage])
    result = service.resolve_paths(config, "/temp_strm/tv/示例", "/temp_strm")
    assert result["mediaPath"] == "/temp_strm/tv/示例"
    assert result["scanPath"] == "/temp_strm/tv/示例"


def test_resolve_saved_local_strm_path() -> None:
    service = StorageService(Settings(), DummyOpenList())  # type: ignore[arg-type]
    storage = Storage(None, "Strm", "/temp_strm")
    storage.save_to_local = True
    storage.save_virtual_root = "/local-strm"
    storage.auto_flatten_prefix = "source"
    config = StorageConfig(strm_storages=[storage])
    result = service.resolve_paths(config, "/temp_strm/tv/示例", "/temp_strm")
    assert result["mediaPath"] == "/local-strm/source/tv/示例"


def test_enrich_discovered_tv_folder_with_category_and_year() -> None:
    folder = StorageService._enrich_folder(
        {
            "name": "百花杀 (2026)",
            "scanPath": "/temp_strm/TV/国产剧/百花杀 (2026)",
            "mediaPath": "/temp_strm/TV/国产剧/百花杀 (2026)",
        },
        "/temp_strm",
    )

    assert folder["sourcePath"] == "/temp_strm/TV/国产剧/百花杀 (2026)"
    assert folder["typeDir"] == "TV"
    assert folder["mediaType"] == "tv"
    assert folder["category"] == "国产剧"
    assert folder["title"] == "百花杀"
    assert folder["year"] == "2026"


def test_enrich_discovered_movie_folder_with_chinese_type_alias() -> None:
    folder = StorageService._enrich_folder(
        {
            "name": "示例电影",
            "scanPath": "/strm/电影/华语电影/示例电影",
            "mediaPath": "/strm/电影/华语电影/示例电影",
        }
    )

    assert folder["mediaType"] == "movie"
    assert folder["category"] == "华语电影"


@pytest.mark.anyio
async def test_configured_root_recursively_discovers_media_folders() -> None:
    service = StorageService(
        Settings(list_root="/strm"),
        DiscoveryOpenList(),  # type: ignore[arg-type]
    )

    folders = await service.list_media_folders()

    assert len(folders) == 1
    assert folders[0]["sourcePath"] == "/strm/tv/国产剧/百花杀 (2026)"
    assert folders[0]["typeDir"] == "tv"
    assert folders[0]["category"] == "国产剧"


@pytest.mark.anyio
async def test_media_options_follow_two_level_directory_structure() -> None:
    openlist = DiscoveryOpenList()
    service = StorageService(
        Settings(list_root="/strm"),
        openlist,  # type: ignore[arg-type]
    )

    types = await service.list_media_options()
    assert openlist.listed_paths == ["/strm"]

    openlist.listed_paths.clear()
    categories = await service.list_media_options("tv")
    assert openlist.listed_paths == ["/strm/tv"]

    openlist.listed_paths.clear()
    resources = await service.list_media_options("tv", "国产剧")
    assert openlist.listed_paths == ["/strm/tv/国产剧"]

    assert [entry["name"] for entry in types["entries"]] == ["tv"]
    assert [entry["name"] for entry in categories["entries"]] == ["国产剧"]
    assert resources["folders"][0]["sourcePath"] == "/strm/tv/国产剧/百花杀 (2026)"


@pytest.mark.anyio
async def test_media_discovery_requires_read_only_source_root() -> None:
    service = StorageService(Settings(list_root=""), DummyOpenList())  # type: ignore[arg-type]

    with pytest.raises(AppError, match="只读源 STRM 根目录") as error:
        await service.list_media_folders()

    assert error.value.status_code == 400


@pytest.mark.anyio
async def test_empty_openlist_content_is_treated_as_an_empty_directory() -> None:
    service = StorageService(
        Settings(list_root="/strm"),
        EmptyContentOpenList(),  # type: ignore[arg-type]
    )

    assert await service.list_media_folders() == []
