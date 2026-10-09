"""Health endpoints.

/health/live   process is alive; touches nothing (platform liveness probe)
/health/ready  required dependencies reachable; read-only, no records written
/health        latest snapshot written periodically by the memory worker, plus worker liveness
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.api.deps import ContainerDep
from app.db.repositories import memory, observability
from app.domain.contracts import DependencyHealth, HealthResponse, ReadinessResponse
from app.services.health_service import MEMORY_WORKER_COMPONENT, probe

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(container: ContainerDep, response: Response) -> ReadinessResponse:
    """Supabase is required to serve any request; everything else degrades gracefully."""
    supabase = await probe(container.db.ping, limit_seconds=3)
    checks: dict[str, Any] = {"supabase": "ok" if supabase.status == "healthy" else "failed"}
    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)


@router.get("/health", response_model=HealthResponse)
async def health(container: ContainerDep) -> HealthResponse:
    settings = container.settings
    async with container.db.transaction() as conn:
        snapshot = await observability.latest_health_snapshot(conn)
        heartbeat = await memory.latest_heartbeat(conn, MEMORY_WORKER_COMPONENT)
        counts = await memory.outbox_counts(conn)

    if heartbeat is None:
        worker = DependencyHealth(status="unhealthy", detail="no heartbeat recorded")
    elif heartbeat["age_seconds"] > settings.worker_heartbeat_stale_seconds:
        worker = DependencyHealth(
            status="unhealthy", detail=f"last heartbeat {heartbeat['age_seconds']}s ago"
        )
    else:
        worker = DependencyHealth(status="healthy")

    dependencies = {
        name: DependencyHealth(status=dep["status"])
        for name, dep in (snapshot["dependencies"] if snapshot else {}).items()
    }
    overall = snapshot["overall_status"] if snapshot else "unknown"
    if overall == "healthy" and worker.status != "healthy":
        overall = "degraded"

    return HealthResponse(
        status=overall,
        version=__version__,
        environment=settings.app_env,
        snapshot_at=snapshot["checked_at"] if snapshot else None,
        dependencies=dependencies,
        memory_jobs=counts,
        worker=worker,
    )
