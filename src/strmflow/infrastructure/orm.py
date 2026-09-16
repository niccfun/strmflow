from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MediaItemRow(Base):
    __tablename__ = "media_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(String(600), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    generated_path: Mapped[str] = mapped_column(String(600), nullable=False)
    target_dir: Mapped[str] = mapped_column(String(600), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    year: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="未分类")
    media_type: Mapped[str] = mapped_column(String(20), nullable=False, default="tv")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ongoing")
    total_episodes: Mapped[int | None] = mapped_column(Integer)
    season: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    update_schedule: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    baidu_link: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    synced_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TransferJobRow(Base):
    __tablename__ = "transfer_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    destination: Mapped[str] = mapped_column(String(1000), nullable=False)
    command: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    job_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="")


class AppMetadataRow(Base):
    __tablename__ = "app_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class MediaProbeRow(Base):
    __tablename__ = "media_probes"

    target_path: Mapped[str] = mapped_column(String(1000), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
