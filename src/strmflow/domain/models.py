from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Storage:
    id: int | str | None
    driver: str
    mount_path: str
    addition: dict[str, Any] = field(default_factory=dict)
    save_to_local: bool = False
    save_virtual_root: str = ""
    source_roots: list[str] = field(default_factory=list)
    auto_flatten_prefix: str = ""


@dataclass(slots=True)
class StorageConfig:
    local_storages: list[Storage] = field(default_factory=list)
    strm_storages: list[Storage] = field(default_factory=list)
