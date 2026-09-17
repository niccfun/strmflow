from __future__ import annotations

import json
import logging
import re
from collections import Counter, deque
from datetime import UTC, datetime
from threading import Lock
from typing import Any

_SENSITIVE_FIELD_NAMES = {
    "authorization",
    "cookie",
    "setcookie",
    "password",
    "passwd",
    "pwd",
    "apikey",
    "apihash",
    "accesstoken",
    "refreshtoken",
    "session",
    "sessionid",
    "strmsession",
    "openlisttoken",
    "embyapikey",
    "telegramapihash",
    "turnstilesecretkey",
    "extractcode",
    "webhookurl",
}
_HEADER_SECRET = re.compile(r"(?im)\b(authorization|x-emby-token|cookie|set-cookie)\s*:\s*[^\r\n]+")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|api[_-]?key|api[_-]?hash|token|"
    r"password|passwd|pwd|session(?:_id)?|key)=)[^&#\s]+"
)
_JSON_SECRET = re.compile(
    r"""(?ix)
    (["']?(?:authorization|password|passwd|pwd|api[_-]?key|api[_-]?hash|
       access[_-]?token|refresh[_-]?token|session(?:_id)?|strm_session|
       openlist_token|emby_api_key|telegram_api_hash|turnstile_secret_key|
       extract[_-]?code|webhook_url)["']?\s*[:=]\s*)
    (["'])(.*?)(\2)
    """
)
_CLI_SECRET = re.compile(
    r"(?i)(\s(?:--password|--passwd|--pwd|--extract-code|--session-id|-p)\s+)(\S+)"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")


def redact_sensitive_text(value: object) -> str:
    """Remove common credentials from raw or third-party log messages."""
    text = str(value)
    text = _HEADER_SECRET.sub(lambda match: f"{match.group(1)}: [已隐藏]", text)
    text = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}[已隐藏]", text)
    text = _JSON_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[已隐藏]{match.group(4)}", text
    )
    text = _CLI_SECRET.sub(lambda match: f"{match.group(1)}[已隐藏]", text)
    return _URL_USERINFO.sub(lambda match: f"{match.group(1)}[已隐藏]@", text)


def _normalized_field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def redact_sensitive_value(value: Any, *, field_name: str = "") -> Any:
    """Recursively redact structured log details without changing their shape."""
    if _normalized_field_name(field_name) in _SENSITIVE_FIELD_NAMES:
        return "[已隐藏]" if value is not None and value != "" else value
    if isinstance(value, dict):
        return {
            key: redact_sensitive_value(item, field_name=str(key)) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


class _RawLogHandler(logging.Handler):
    def __init__(self, store: RuntimeLogStore) -> None:
        super().__init__(level=logging.NOTSET)
        self.store = store
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            captured = record.__dict__.setdefault("_strmflow_raw_stores", set())
            token = id(self.store)
            if token in captured:
                return
            captured.add(token)
            self.store.capture_raw(
                self.format(record),
                level=record.levelname.casefold(),
                logger=record.name,
                created=record.created,
            )
        except Exception:  # noqa: BLE001  # pragma: no cover - logging must stay isolated
            self.handleError(record)


class RuntimeLogStore:
    """Bounded structured events plus the unmodified Python logging stream."""

    def __init__(self, capacity: int = 2_000, max_line_chars: int = 16_384) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._raw_lines: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._next_raw_id = 1
        self._max_line_chars = max(1_024, max_line_chars)
        self._lock = Lock()
        self._handler = _RawLogHandler(self)
        self._attached_loggers: list[logging.Logger] = []

    def attach(self) -> None:
        if self._attached_loggers:
            return
        for name in ("", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.addHandler(self._handler)
            self._attached_loggers.append(logger)

    def detach(self) -> None:
        for logger in self._attached_loggers:
            logger.removeHandler(self._handler)
        self._attached_loggers.clear()

    def capture_raw(
        self,
        text: str,
        *,
        level: str = "info",
        logger: str = "",
        created: float | None = None,
    ) -> dict[str, Any]:
        value = redact_sensitive_text(text)
        if len(value) > self._max_line_chars:
            value = value[: self._max_line_chars] + " … [单条日志已截断]"
        timestamp = (
            datetime.fromtimestamp(created, UTC) if created is not None else datetime.now(UTC)
        )
        with self._lock:
            line = {
                "id": self._next_raw_id,
                "time": timestamp.isoformat(),
                "level": level,
                "logger": logger,
                "text": value,
            }
            self._next_raw_id += 1
            self._raw_lines.append(line)
            return dict(line)

    def add(
        self,
        *,
        category: str,
        message: str,
        level: str = "info",
        **details: Any,
    ) -> dict[str, Any]:
        safe_details = {
            key: redact_sensitive_value(value, field_name=key) for key, value in details.items()
        }
        with self._lock:
            entry = {
                "id": self._next_id,
                "time": datetime.now(UTC).isoformat(),
                "category": category,
                "level": level,
                "message": redact_sensitive_text(message),
                **safe_details,
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
        # The management API's access logger is provided by Uvicorn itself, but
        # the embedded 302 gateway intentionally disables Uvicorn access logs.
        # Mirror gateway requests so playback diagnostics are visible in Docker.
        if (
            entry.get("method")
            and entry.get("statusCode") is not None
            and entry.get("category") != "gateway302"
        ):
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

    def raw_lines(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            lines = list(self._raw_lines)
        return [dict(line) for line in lines[-limit:]]

    def clear(self) -> int:
        with self._lock:
            count = len(self._raw_lines) or len(self._entries)
            self._entries.clear()
            self._raw_lines.clear()
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
    if path.startswith("/api/telegram"):
        return "telegram"
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
