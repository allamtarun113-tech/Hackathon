"""Sentry initialisation with credential scrubbing."""

from __future__ import annotations

from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.core.config import Settings
from app.core.context import current_request_id
from app.core.redaction import redact_text


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    request = event.get("request") or {}
    headers = request.get("headers") or {}
    for header in list(headers):
        if header.lower() in {"authorization", "cookie", "x-hindsight-signature", "x-hindsight-signature-v2"}:
            headers[header] = "[REDACTED]"
    if isinstance(request.get("data"), str):
        request["data"] = redact_text(request["data"]).text
    for exception in (event.get("exception") or {}).get("values", []):
        if isinstance(exception.get("value"), str):
            exception["value"] = redact_text(exception["value"]).text
    request_id = current_request_id()
    if request_id:
        event.setdefault("tags", {})["request_id"] = request_id
    return event


def init_sentry(settings: Settings, *, component: str) -> None:
    if not settings.sentry_enabled:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.app_env,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        before_send=_before_send,  # type: ignore[arg-type]
    )
    sentry_sdk.set_tag("component", component)


def capture(error: BaseException, **tags: str) -> None:
    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            scope.set_tag(key, value)
        sentry_sdk.capture_exception(error)
