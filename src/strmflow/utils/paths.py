from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urlsplit

from strmflow.core.errors import AppError


def normalize_virtual_path(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = "/" + raw.lstrip("/")
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    return path


def validate_virtual_path(value: object, label: str = "路径") -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        source = parsed.path if parsed.scheme in {"http", "https"} and parsed.netloc else raw
        source = unquote(source)
    except ValueError:
        source = raw
    path = normalize_virtual_path(source)
    if (
        not raw
        or len(path) > 600
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise AppError(400, f"{label}不合法")
    return path


def join_virtual_path(root: object, *parts: object) -> str:
    result = normalize_virtual_path(root)
    for part in parts:
        child = str(part or "").strip("/")
        if child:
            result = normalize_virtual_path(posixpath.join(result, child))
    return result


def relative_virtual_path(root: object, path: object) -> str | None:
    parent = normalize_virtual_path(root)
    child = normalize_virtual_path(path)
    if child == parent:
        return ""
    prefix = "/" if parent == "/" else f"{parent}/"
    return child[len(prefix) :] if child.startswith(prefix) else None


def path_base(path: object) -> str:
    value = normalize_virtual_path(path)
    return "" if value == "/" else value.rsplit("/", 1)[-1]


def path_dir(path: object) -> str:
    value = normalize_virtual_path(path)
    return normalize_virtual_path(posixpath.dirname(value))


def validate_folder_name(value: object) -> str:
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise AppError(400, "目录名称不合法")
    return name
