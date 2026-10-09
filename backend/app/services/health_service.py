"""Dependency probes shared by the readiness endpoint and the worker's periodic health snapshots."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import Settings
from app.db.pool import Database
from app.domain.contracts import DependencyHealth
from app.memory.client import HindsightGateway
from app.storage.supabase_storage import SupabaseStorage

CRITICAL_DEPENDENCIES = ("supabase",)
MEMORY_WORKER_COMPONENT = "memory_worker"


async def probe(check: Callable[[], Awaitable[Any]], limit_seconds: float = 5.0) -> DependencyHealth:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check(), timeout=limit_seconds)
    except Exception as error:
        return DependencyHealth(status="unhealthy", detail=type(error).__name__)
    return DependencyHealth(status="healthy", latency_ms=int((time.perf_counter() - started) * 1000))


def configured(enabled: bool) -> DependencyHealth:
    return DependencyHealth(status="healthy" if enabled else "not_configured")


async def probe_hindsight(hindsight: HindsightGateway) -> DependencyHealth:
    """The production bank must exist, and observation consolidation must not be silently failing."""
    started = time.perf_counter()
    try:
        stats = await asyncio.wait_for(hindsight.bank_stats(), timeout=10)
    except Exception as error:
        return DependencyHealth(status="unhealthy", detail=type(error).__name__)
    latency = int((time.perf_counter() - started) * 1000)
    if stats.failed_consolidation:
        return DependencyHealth(
            status="degraded",
            latency_ms=latency,
            detail=f"{stats.failed_consolidation} memories failed observation consolidation",
        )
    return DependencyHealth(status="healthy", latency_ms=latency)


async def probe_all(
    settings: Settings, db: Database, storage: SupabaseStorage, hindsight: HindsightGateway
) -> dict[str, DependencyHealth]:
    supabase, storage_health, hindsight_health = await asyncio.gather(
        probe(db.ping), probe(storage.ping), probe_hindsight(hindsight)
    )
    return {
        "supabase": supabase,
        "supabase_storage": storage_health,
        "hindsight": hindsight_health,
        "groq": configured(bool(settings.groq_api_key.get_secret_value())),
        "langfuse": configured(settings.langfuse_enabled),
        "sentry": configured(settings.sentry_enabled),
    }


def overall_status(dependencies: dict[str, DependencyHealth]) -> str:
    if any(
        dependencies[name].status == "unhealthy" for name in CRITICAL_DEPENDENCIES if name in dependencies
    ):
        return "unhealthy"
    if any(dep.status in ("unhealthy", "degraded") for dep in dependencies.values()):
        return "degraded"
    return "healthy"
