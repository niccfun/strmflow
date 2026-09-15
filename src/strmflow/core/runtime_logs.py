from __future__ import annotations

import json
import logging
from collections import Counter, deque
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class RuntimeLogStore:
    """Thread-safe in-memory ring buffer for recent runtime and HTTP events."""

    def __init__(self, capacity: int = 1_000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._lock = Lock()

    def add(
        self,
        *,
        category: str,
        message: str,
        level: str = "info",
        **details: Any,
    ) -> dict[str, Any]:
        with self._lock:
            entry = {
                "id": self._next_id,
                "time": datetime.now(UTC).isoformat(),
                "category": category,
                "level": level,
                "message": message,
                **details,
            }
            self._next_id += 1
            self._entries.appendleft(entry)
            saved = dict(entry)
        self._write_console(saved)
        return saved

    @staticmethod
    def _write_console(entry: dict[str, Any]) -> None:
        """Mirror application events to Uvicorn so ``docker logs`` is useful.

        HTTP access events are already emitted by Uvicorn and remain available in
        the in-memory log page.  Skipping them here prevents every request from
        appearing twice in the container output.
        """
        if entry.get("method") and entry.get("statusCode") is not None:
            return
        ignored = {"id", "time", "category", "level", "message"}
        details = {key: value for key, value in entry.items() if key not in ignored}
        suffix = ""
        if details:
            suffix = " · " + json.dumps(
                details,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        level = str(entry.get("level") or "info").casefold()
        log_level = {
            "error": logging.ERROR,
            "warning": logging.WARNING,
        }.get(level, logging.INFO)
        logging.getLogger("uvicorn.error").log(
            log_level,
            "[%s] %s%s",
            entry.get("category") or "system",
            entry.get("message") or "",
            suffix,
        )

    def list(self, *, limit: int = 300, category: str = "") -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._entries)
        if category:
            entries = [entry for entry in entries if entry["category"] == category]
        return [dict(entry) for entry in entries[:limit]]

    def categories(self) -> dict[str, int]:
        with self._lock:
            counts = Counter(entry["category"] for entry in self._entries)
        return dict(sorted(counts.items()))

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        return count


def request_category(path: str) -> str:
    if path in {"/login", "/api/login", "/api/logout"}:
        return "auth"
    if path.startswith(("/api/items/scan", "/api/scan")):
        return "scan"
    if path.startswith("/api/emby302"):
        return "gateway302"
    if path.startswith("/api/bdpan"):
        return "bdpan"
    if path.startswith("/api/notifications"):
        return "notification"
    if path.startswith("/api/emby"):
        return "sync"
    if path.startswith("/api/settings") or path == "/api/config":
        return "settings"
    if path.startswith(("/api/media", "/api/items")):
        return "media"
    if path.startswith("/api/transfers"):
        return "transfer"
    if path.startswith(("/api/health", "/api/status")):
        return "system"
    return "http"


def status_level(status_code: int) -> str:
    if status_code >= 500:
        return "error"
    if status_code >= 400:
        return "warning"
    if status_code >= 300:
        return "redirect"
    return "success"
