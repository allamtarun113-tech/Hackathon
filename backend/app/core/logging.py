"""Structured JSON logging.

Every log line carries the request ID plus any bound fields (conversation_id,
incident_id, service, agent_node, duration_ms, outcome, error_type). A final
processor scrubs credentials so a careless log call cannot leak a secret.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, FilteringBoundLogger

from app.core.context import current_request_id
from app.core.redaction import redact_text

_SENSITIVE_KEYS = {"authorization", "api_key", "secret", "password", "token", "cookie", "dsn"}
_SENSITIVE_SUFFIXES = ("_key", "_secret", "_password", "_token")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


def _add_request_id(_: Any, __: str, event: EventDict) -> EventDict:
    request_id = current_request_id()
    if request_id and "request_id" not in event:
        event["request_id"] = request_id
    return event


def _scrub(_: Any, __: str, event: EventDict) -> EventDict:
    for key, value in list(event.items()):
        if _is_sensitive(key):
            event[key] = "[REDACTED]"
        elif isinstance(value, str):
            event[key] = redact_text(value).text
    return event


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_id,
            structlog.processors.format_exc_info,
            _scrub,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    # Route stdlib loggers (uvicorn, httpx) through the same level.
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(message)s")


def get_logger(name: str) -> FilteringBoundLogger:
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
