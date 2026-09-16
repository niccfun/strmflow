from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class LoginRequest(ApiModel):
    username: str
    password: str
    turnstile_token: str | None = None


class MediaItemInput(ApiModel):
    id: str | None = None
    source_path: str
    generated_path: str | None = None
    title: str = ""
    year: str | int | None = ""
    category: str = ""
    media_type: Literal["tv", "movie"] = "tv"
    status: Literal["ongoing", "completed"] = "ongoing"
    total_episodes: int | None = Field(default=None, gt=0)
    season: int = Field(default=1, ge=1, le=99)
    update_schedule: str = ""
    baidu_link: str = ""

    @field_validator("total_episodes", mode="before")
    @classmethod
    def empty_episode_count(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("season", mode="before")
    @classmethod
    def empty_season(cls, value: object) -> object:
        return 1 if value == "" or value is None else value


class IdRequest(ApiModel):
    id: str


class ScanRequest(ApiModel):
    name: str
    scan_path: str | None = None
    media_path: str | None = None


class PublishRequest(ApiModel):
    id: str | None = None
    name: str | None = None
    scan_path: str | None = None
    media_path: str | None = None
    title: str = ""
    year: str | int | None = ""
    category: str = ""
    media_type: Literal["tv", "movie"] = "tv"
    season: int = Field(default=1, ge=1, le=99)
    rename_plan: list[dict[str, str]] | None = None


class TransferCreateRequest(ApiModel):
    provider: str = "bdpan"
    share_url: str
    destination: str
    extract_code: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ItemTransferRequest(ApiModel):
    provider: str = "bdpan"
    destination: str
    extract_code: str = ""


class PathConfigUpdate(ApiModel):
    list_root: str
    emby_strm_root: str


class Emby302ConfigUpdate(ApiModel):
    enabled: bool = False
    emby_url: str | None = Field(default=None, max_length=2_048)
    openlist_url: str | None = Field(default=None, max_length=2_048)
    host: str = Field(default="0.0.0.0", min_length=1, max_length=255)
    port: int = Field(default=18096, ge=1, le=65_535)
    cache_ttl: int = Field(default=21_600, ge=1, le=86_400)
    cache_max: int = Field(default=1_000, ge=1, le=100_000)
    body_buffer_max: int = Field(default=1_048_576, ge=1_024, le=107_374_182_400)
    timeout_ms: int = Field(default=30_000, ge=1_000, le=600_000)

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        host = value.strip()
        if not host or any(character.isspace() for character in host):
            raise ValueError("监听地址格式不正确")
        return host

    @field_validator("emby_url", "openlist_url")
    @classmethod
    def valid_upstream_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = value.strip().rstrip("/")
        if not url:
            return ""
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("容器地址需要是完整的 HTTP 或 HTTPS URL")
        return url


class BdpanAutomationConfigUpdate(ApiModel):
    enabled: bool = False
    binary: str = Field(default="bdpan", min_length=1, max_length=500)
    check_interval_minutes: int = Field(default=10, ge=5, le=1440)
    save_root: str = Field(default="StrmFlow", min_length=1, max_length=700)
    settle_seconds: int = Field(default=90, ge=30, le=1800)
    max_new_items: int = Field(default=20, ge=1, le=100)


class BdpanLoginCompleteRequest(ApiModel):
    code: str = Field(min_length=32, max_length=32, pattern=r"^[A-Fa-f0-9]{32}$")


class BdpanLoginStartRequest(ApiModel):
    accepted: bool = False


class BdpanShareInspectRequest(ApiModel):
    share_url: str = Field(min_length=1, max_length=2_048)
    extract_code: str = Field(default="", max_length=16)


class BdpanShareImportRequest(ApiModel):
    preview_id: str = Field(min_length=16, max_length=200)
    candidate_id: str = Field(min_length=8, max_length=200)
    type_dir: str = Field(min_length=1, max_length=255)
    category: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=100)
    year: str | int | None = ""
    media_type: Literal["tv", "movie"] = "tv"
    status: Literal["ongoing", "completed"] = "ongoing"
    total_episodes: int | None = Field(default=None, gt=0)
    season: int = Field(default=1, ge=1, le=99)
    update_schedule: str = Field(default="", max_length=500)

    @field_validator("total_episodes", mode="before")
    @classmethod
    def empty_import_episode_count(cls, value: object) -> object:
        return None if value == "" else value


class WecomWebhookConfigUpdate(ApiModel):
    webhook_url: str | None = Field(default=None, max_length=2_048)
    episode_update_enabled: bool = False
    link_invalid_enabled: bool = False


class MediaProbeConfigUpdate(ApiModel):
    daily_enabled: bool = False
    scan_time: str = Field(default="03:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TransferJob(ApiModel):
    id: str
    provider: str
    status: Literal["queued", "running", "succeeded", "failed"]
    destination: str
    command: list[str]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
