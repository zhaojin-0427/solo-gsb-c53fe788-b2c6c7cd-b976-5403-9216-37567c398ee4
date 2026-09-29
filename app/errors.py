"""Stable, deterministic application errors.

All 4xx responses use one schema::

    {"error": {"type": "...", "details": [{"code", "path", "message"}, ...]}}

``details`` is sorted by (path, code), so identical invalid requests always
produce byte-identical error bodies.
"""
from __future__ import annotations

from typing import Any, List


class ErrorItem:
    __slots__ = ("code", "path", "message")

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class ValidationError(Exception):
    def __init__(self, errors: List[ErrorItem]) -> None:
        self.errors = sorted(errors, key=lambda e: (e.path, e.code))
        super().__init__("; ".join(f"{e.path}:{e.code}" for e in self.errors))


class NotFoundError(Exception):
    def __init__(self, resource: str, key: str, path: str = "") -> None:
        self.resource = resource
        self.key = key
        self.path = path
        super().__init__(f"{resource} not found: {key}")

    def error_item(self) -> ErrorItem:
        return ErrorItem(
            code="NOT_FOUND",
            path=self.path or f".{self.resource}:{self.key}",
            message=f"{self.resource} '{self.key}' does not exist")


class ConflictError(Exception):
    def __init__(self, code: str, path: str, message: str) -> None:
        self.item = ErrorItem(code=code, path=path, message=message)
        super().__init__(message)


def error_body(err_type: str, details: list[dict[str, Any] | ErrorItem]) -> dict:
    out = []
    for d in details:
        out.append(d.as_dict() if isinstance(d, ErrorItem) else d)
    out.sort(key=lambda d: (d["path"], d["code"]))
    return {"error": {"type": err_type, "details": out}}
