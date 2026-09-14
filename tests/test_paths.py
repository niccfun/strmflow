import pytest

from strmflow.core.errors import AppError
from strmflow.utils.paths import (
    join_virtual_path,
    normalize_virtual_path,
    relative_virtual_path,
    validate_virtual_path,
)


def test_virtual_paths() -> None:
    assert normalize_virtual_path("//tv///国产剧/") == "/tv/国产剧"
    assert join_virtual_path("/tv", "国产剧", "剧名") == "/tv/国产剧/剧名"
    assert relative_virtual_path("/tv", "/tv/国产剧/剧名") == "国产剧/剧名"
    assert relative_virtual_path("/movie", "/tv/国产剧") is None
    assert validate_virtual_path("https://example.com/tv/%E7%A4%BA%E4%BE%8B") == "/tv/示例"


def test_validate_virtual_path_rejects_parent_segments() -> None:
    with pytest.raises(AppError, match="不合法"):
        validate_virtual_path("/tv/../secret")
