"""Request-scoped context propagated through logs, traces and audit events."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def current_request_id() -> str | None:
    return request_id_var.get()


def new_id(prefix: str) -> str:
    """Short, sortable-enough identifier for rows created at runtime."""
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"
