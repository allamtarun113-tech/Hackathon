"""HTTP middleware: request IDs, body-size limits, rate limiting and access logs.

Implemented as pure ASGI middleware so streaming (SSE) responses are not buffered.
"""

from __future__ import annotations

import json
import time

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.ratelimit import client_ip
from app.core.context import new_request_id, request_id_var
from app.core.logging import get_logger

log = get_logger("http")

_RATE_LIMIT_EXEMPT = ("/health", "/webhooks/")


async def _send_json(
    send: Send, status: int, body: dict[str, object], headers: list[tuple[bytes, bytes]]
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *headers],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    """Assigns a request ID (honouring a well-formed incoming X-Request-ID) and logs each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode()
        request_id = incoming if incoming.isascii() and 8 <= len(incoming) <= 64 else new_request_id()
        token = request_id_var.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    """Rejects bodies above the limit, using Content-Length when present and counting bytes otherwise."""

    def __init__(self, app: ASGIApp, *, default_limit: int, upload_limit: int) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.upload_limit + 64 * 1024 if scope["path"] == "/files" else self.default_limit
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await _send_json(
                send, 413, {"error": {"code": "payload_too_large", "message": "Request too large."}}, []
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _send_json(
                send, 413, {"error": {"code": "payload_too_large", "message": "Request too large."}}, []
            )


class _BodyTooLarge(Exception):
    pass


class RateLimitMiddleware:
    """Limits mutating requests per client using the shared Postgres-backed limiter."""

    def __init__(self, app: ASGIApp, *, trusted_proxy_hops: int) -> None:
        self.app = app
        self.trusted_proxy_hops = trusted_proxy_hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in ("GET", "HEAD", "OPTIONS")
            or scope["path"].startswith(_RATE_LIMIT_EXEMPT)
        ):
            await self.app(scope, receive, send)
            return

        limiter = getattr(scope["app"].state, "rate_limiter", None)
        if limiter is None:  # not started yet
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        peer = scope["client"][0] if scope.get("client") else None
        key = client_ip(
            headers.get(b"x-forwarded-for", b"").decode() or None, peer, trusted_hops=self.trusted_proxy_hops
        )
        allowed, retry_after = await limiter.allow(f"ip:{key}")
        if not allowed:
            await _send_json(
                send,
                429,
                {"error": {"code": "rate_limited", "message": "Too many requests. Please retry shortly."}},
                [(b"retry-after", str(retry_after).encode())],
            )
            return
        await self.app(scope, receive, send)
