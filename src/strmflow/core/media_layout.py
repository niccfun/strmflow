from __future__ import annotations

from typing import Any

from strmflow.utils.paths import join_virtual_path, normalize_virtual_path, relative_virtual_path

UNCATEGORIZED = "未分类"
OTHER_TYPE_DIR = "其它"

BUILTIN_MEDIA_LAYOUT: tuple[dict[str, Any], ...] = (
    {
        "name": "电影",
        "mediaType": "movie",
        "categories": ("动画电影", "华语电影", "外语电影"),
    },
    {
        "name": "电视剧",
        "mediaType": "tv",
        "categories": (
            "国漫",
            "日番",
            "纪录片",
            "儿童",
            "综艺",
            "国产剧",
            "欧美剧",
            "日韩剧",
            UNCATEGORIZED,
        ),
    },
    {
        "name": OTHER_TYPE_DIR,
        "mediaType": "tv",
        # “其它”直接保存媒体目录，不再增加真实的二级目录。前端和 API
        # 使用“未分类”作为配置占位值，以保持现有媒体模型兼容。
        "categories": (UNCATEGORIZED,),
        "flat": True,
    },
)

_ALIASES = {
    "movie": "电影",
    "movies": "电影",
    "影片": "电影",
    "电影": "电影",
    "tv": "电视剧",
    "电视剧": "电视剧",
    "剧集": "电视剧",
    "shows": "电视剧",
    "show": "电视剧",
    "other": OTHER_TYPE_DIR,
    "others": OTHER_TYPE_DIR,
    "其它": OTHER_TYPE_DIR,
    "其他": OTHER_TYPE_DIR,
}


def builtin_type_entries() -> list[dict[str, Any]]:
    return [
        {
            "name": str(item["name"]),
            "mediaType": str(item["mediaType"]),
            "categoryRequired": not bool(item.get("flat")),
        }
        for item in BUILTIN_MEDIA_LAYOUT
    ]


def canonical_type_dir(value: object, media_type: str = "tv") -> str:
    raw = str(value or "").strip()
    alias = _ALIASES.get(raw.casefold())
    if alias:
        return alias
    return "电影" if media_type == "movie" else "电视剧"


def layout_for_type(value: object) -> dict[str, Any] | None:
    raw = str(value or "").strip()
    canonical = _ALIASES.get(raw.casefold(), raw)
    return next(
        (item for item in BUILTIN_MEDIA_LAYOUT if item["name"] == canonical),
        None,
    )


def media_type_for_type(value: object) -> str:
    layout = layout_for_type(value)
    return str(layout["mediaType"]) if layout else "tv"


def category_entries(value: object) -> list[dict[str, Any]]:
    layout = layout_for_type(value)
    if not layout:
        return []
    flat = bool(layout.get("flat"))
    return [
        {
            "name": str(category),
            "label": "无二级分类" if flat else str(category),
            "virtual": flat,
        }
        for category in layout["categories"]
    ]


def normalize_category(type_dir: object, category: object) -> str | None:
    layout = layout_for_type(type_dir)
    if not layout:
        return None
    if layout.get("flat"):
        return UNCATEGORIZED
    raw = str(category or "").strip()
    return raw if raw in layout["categories"] else None


def media_parent_path(root: str, type_dir: object, category: object = "") -> str:
    layout = layout_for_type(type_dir)
    if not layout:
        return join_virtual_path(root, canonical_type_dir(type_dir), str(category or ""))
    if layout.get("flat"):
        return join_virtual_path(root, str(layout["name"]))
    return join_virtual_path(root, str(layout["name"]), str(category or ""))


def media_resource_path(root: str, type_dir: object, category: object, name: str) -> str:
    return join_virtual_path(media_parent_path(root, type_dir, category), name)


def type_dir_from_source(root: str, source_path: str, media_type: str = "tv") -> str:
    relative = relative_virtual_path(
        normalize_virtual_path(root), normalize_virtual_path(source_path)
    )
    first = str(relative or "").split("/", 1)[0]
    return canonical_type_dir(first, media_type)


def builtin_directory_paths(root: str) -> list[str]:
    normalized = normalize_virtual_path(root)
    paths = [normalized]
    for item in BUILTIN_MEDIA_LAYOUT:
        type_path = join_virtual_path(normalized, str(item["name"]))
        paths.append(type_path)
        if item.get("flat"):
            continue
        paths.extend(join_virtual_path(type_path, str(value)) for value in item["categories"])
    return paths
