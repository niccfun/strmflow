from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from strmflow.core.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self.url = settings.database_url
        self._ensure_sqlite_parent()
        self.engine: AsyncEngine = create_async_engine(self.url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._upgrade)

    async def close(self) -> None:
        await self.engine.dispose()

    def _ensure_sqlite_parent(self) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not self.url.startswith(prefix) or self.url.endswith(":memory:"):
            return
        raw_path = self.url[len(prefix) :].split("?", 1)[0]
        path = Path("/" + raw_path.lstrip("/")) if raw_path.startswith("/") else Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)

    def _upgrade(self) -> None:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parent.parent / "migrations")
        )
        config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        config.attributes["database_url"] = self.url
        command.upgrade(config, "head")


@event.listens_for(Engine, "connect")
def configure_sqlite(connection, _record) -> None:
    """Enable concurrency and integrity settings on SQLite DB-API connections."""
    module = type(connection).__module__
    if "sqlite" not in module and "aiosqlite" not in module:
        return
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
