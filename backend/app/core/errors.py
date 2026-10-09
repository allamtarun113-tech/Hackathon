"""Domain exceptions and their HTTP mapping."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.context import current_request_id
from app.core.logging import get_logger

log = get_logger(__name__)


class PagerMindError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(PagerMindError):
    status_code = 404
    code = "not_found"


class ValidationFailed(PagerMindError):
    status_code = 422
    code = "validation_failed"


class GuardrailRejected(PagerMindError):
    status_code = 422
    code = "guardrail_rejected"


class Unauthorized(PagerMindError):
    status_code = 401
    code = "unauthorized"


class DependencyUnavailable(PagerMindError):
    status_code = 503
    code = "dependency_unavailable"


def _body(error: PagerMindError) -> dict[str, object]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
            "request_id": current_request_id(),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PagerMindError)
    async def _handle_domain_error(_: Request, error: PagerMindError) -> JSONResponse:
        log.warning("request_failed", error_type=error.code, error=error.message)
        return JSONResponse(status_code=error.status_code, content=_body(error))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, error: Exception) -> JSONResponse:
        log.exception("unhandled_exception", error_type=type(error).__name__)
        return JSONResponse(
            status_code=500,
            content=_body(PagerMindError("An unexpected error occurred.")),
        )
