"""Write boundary for privileged endpoints.

PagerMind has no user accounts by design. Endpoints that change canonical incident
data, promote feedback into memory or expose internal records instead require one
deployment-held operator secret, sent as `Authorization: Bearer <token>`. If the
secret is not configured the endpoints are disabled (fail closed).
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.errors import DependencyUnavailable, Unauthorized


def _presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def is_operator(request: Request, settings: Settings) -> bool:
    expected = settings.operator_token.get_secret_value()
    presented = _presented_token(request)
    return bool(expected and presented and hmac.compare_digest(presented.encode(), expected.encode()))


def require_operator(request: Request) -> None:
    settings: Settings = request.app.state.container.settings
    if not settings.operator_token.get_secret_value():
        raise DependencyUnavailable("Operator endpoints are disabled on this deployment.")
    if not is_operator(request, settings):
        raise Unauthorized("A valid operator token is required.")


OperatorOnly = Annotated[None, Depends(require_operator)]
