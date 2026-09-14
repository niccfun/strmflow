from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(self, status_code: int, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


class UpstreamError(AppError):
    def __init__(self, message: str, details: Any = None, status_code: int = 502) -> None:
        super().__init__(status_code, message, details)
