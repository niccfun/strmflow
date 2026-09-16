from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        case_sensitive=False,
        extra="ignore",
    )

    app_user: str = "admin"
    app_password: str = ""
    session_secret: str = ""
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8787

    openlist_url: str = "http://127.0.0.1:5244"
    openlist_web_url: str = ""
    openlist_token: str = ""
    openlist_path_password: str = ""
    openlist_timeout: float = 30.0
    list_root: str = ""
    scan_limit: float = Field(default=2, gt=0)

    emby_strm_root: str = "/local_media/emby-strm"
    media_db_path: str = ""
    emby_url: str = ""
    emby_web_url: str = ""
    emby_api_key: str = ""
    emby_refresh_path: str = "/emby/Library/Refresh"

    emby_302_enabled: bool = False
    emby_302_host: str = "0.0.0.0"
    emby_302_port: int = Field(default=18096, ge=1, le=65_535)
    emby_302_cache_ttl: int = Field(default=21_600, ge=1, le=86_400)
    emby_302_cache_max: int = Field(default=1_000, ge=1, le=100_000)
    emby_302_body_buffer_max: int = Field(default=1_048_576, ge=1_024, le=107_374_182_400)
    emby_302_timeout_ms: int = Field(default=30_000, ge=1_000, le=600_000)

    media_probe_enabled: bool = True
    media_probe_delay_seconds: int = Field(default=10, ge=0, le=300)
    media_probe_timeout: int = Field(default=90, ge=10, le=600)
    app_timezone: str = "Asia/Hong_Kong"

    database_url: str = "sqlite+aiosqlite:///./data/strmflow.db"
    legacy_json_import: bool = True

    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

    bdpan_enabled: bool = False
    bdpan_binary: str = "bdpan"
    bdpan_timeout: int = Field(default=3600, gt=0)
    bdpan_check_interval_minutes: int = Field(default=10, ge=5, le=1440)
    bdpan_save_root: str = "StrmFlow"
    bdpan_settle_seconds: int = Field(default=90, ge=30, le=1800)
    bdpan_max_new_items: int = Field(default=20, ge=1, le=100)
    transfer_job_retention: int = Field(default=200, ge=10, le=10_000)

    @model_validator(mode="after")
    def validate_pairs(self) -> Settings:
        if bool(self.turnstile_site_key) != bool(self.turnstile_secret_key):
            raise ValueError("TURNSTILE_SITE_KEY 和 TURNSTILE_SECRET_KEY 必须同时配置")
        return self

    @property
    def resolved_media_db_path(self) -> str:
        # 保持与 demo 旧版本相同的默认路径，升级后可直接读取已有数据。
        return self.media_db_path or f"{self.emby_strm_root}/.openlist-strm-sync/media.json"

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "web" / "templates"

    def validate_runtime(self) -> None:
        missing = [
            key
            for key, value in {
                "APP_PASSWORD": self.app_password,
                "OPENLIST_URL": self.openlist_url,
                "OPENLIST_TOKEN": self.openlist_token,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"缺少服务配置：{', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
