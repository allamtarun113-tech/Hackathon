"""FastAPI application factory.

There is deliberately no module-level `app`: importing this module has no side effects
(logging, Sentry and tracing are configured only when an app is created).

Run with:  uvicorn --factory app.main:create_app
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.middleware import BodySizeLimitMiddleware, RateLimitMiddleware, RequestContextMiddleware
from app.api.ratelimit import PostgresRateLimiter
from app.api.routes import (
    chat,
    conversations,
    feedback,
    files,
    health,
    incidents,
    memory_jobs,
    reference,
    webhooks,
)
from app.container import Container
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.observability.sentry import init_sentry
from app.observability.tracing import init_tracing, shutdown_tracing

log = get_logger(__name__)


ContainerFactory = Callable[[Settings], Awaitable[Container]]


def create_app(
    settings: Settings | None = None, *, container_factory: ContainerFactory | None = None
) -> FastAPI:
    """`container_factory` lets tests inject fakes for external services."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env != "development")
    init_sentry(settings, component="api")
    init_tracing(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = await (container_factory or Container.create)(settings)
        app.state.rate_limiter = PostgresRateLimiter(
            app.state.container.db, limit=settings.rate_limit_per_minute, window_seconds=60
        )
        log.info("api_started", environment=settings.app_env, version=__version__)
        try:
            yield
        finally:
            await app.state.container.close()
            shutdown_tracing()
            log.info("api_stopped")

    app = FastAPI(
        title="PagerMind API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )

    # Middleware runs outermost-last: request context wraps everything else.
    app.add_middleware(RateLimitMiddleware, trusted_proxy_hops=settings.trusted_proxy_hops)
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=settings.max_request_bytes,
        upload_limit=settings.max_upload_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID", "Authorization"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    for router in (
        health.router,
        reference.router,
        chat.router,
        conversations.router,
        incidents.router,
        feedback.router,
        files.router,
        memory_jobs.router,
        webhooks.router,
    ):
        app.include_router(router)
    return app
