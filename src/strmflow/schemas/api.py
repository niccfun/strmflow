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
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1_024)
    turnstile_token: str | None = Field(default=None, max_length=4_096)


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
    replace_existing: bool = False


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
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("容器地址端口格式不正确") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65_535
        ):
            raise ValueError("容器地址需要是完整的 HTTP 或 HTTPS URL")
        return url


class BdpanAutomationConfigUpdate(ApiModel):
    enabled: bool = False
    tracking_mode: Literal["polling", "hybrid"] = "polling"
    check_interval_minutes: int = Field(default=10, ge=5, le=1440)
    save_root: str = Field(default="video", min_length=1, max_length=700)
    settle_seconds: int = Field(default=90, ge=30, le=1800)
    max_new_items: int = Field(default=20, ge=1, le=100)


class BdpanLoginCompleteRequest(ApiModel):
    code: str = Field(min_length=32, max_length=32, pattern=r"^[A-Fa-f0-9]{32}$")


class BdpanLoginStartRequest(ApiModel):
    accepted: bool = False


class TelegramConfigUpdate(ApiModel):
    enabled: bool = False
    phone: str = Field(default="", max_length=40)
    sources: list[str] = Field(default_factory=list, max_length=100)


class TelegramLoginStartRequest(ApiModel):
    phone: str = Field(min_length=5, max_length=40)


class TelegramLoginCompleteRequest(ApiModel):
    code: str = Field(default="", max_length=20)
    password: str = Field(default="", max_length=256)


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
    episode_image_enabled: bool | None = None
    episode_image_library_ids: list[str] | None = Field(default=None, max_length=100)
    scan_concurrency: int | None = Field(default=None, ge=1, le=32)
    probe_delay_seconds: int | None = Field(default=None, ge=0, le=300)
    probe_timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    episode_image_timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    episode_image_seek_percent: int | None = Field(default=None, ge=5, le=90)
    episode_image_max_width: int | None = Field(default=None, ge=320, le=3840)
    episode_image_jpeg_quality: int | None = Field(default=None, ge=1, le=10)


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
