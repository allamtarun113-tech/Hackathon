"""Apply a Hindsight operation result to an outbox job (shared by the webhook and the reconciler)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.db.pool import Connection
from app.db.repositories import audit
from app.db.repositories import memory as memory_repo
from app.db.repositories.audit import ActorType


async def apply_operation_status(
    conn: Connection,
    job: dict[str, Any],
    *,
    status: str,
    error_message: str | None,
    memory_unit_count: int | None,
    actor: ActorType,
    retry_in: timedelta,
) -> str:
    """Returns the job state after applying the result."""
    if status == "completed":
        final = await memory_repo.complete_job(conn, job["job_id"], memory_unit_count)
        if final is None:
            return "duplicate"
        if final == "retained":
            await memory_repo.mark_source_retained(conn, job)
        await audit.record(
            conn,
            "memory_job.completed" if final == "retained" else "memory_job.needs_review",
            resource_type="memory_outbox",
            resource_id=job["job_id"],
            actor_type=actor,
            details={"memory_unit_count": memory_unit_count, "status": final},
        )
        return final

    if status in ("failed", "cancelled"):
        final = await memory_repo.mark_failed(conn, job["job_id"], error_message or status, retry_in=retry_in)
        await audit.record(
            conn,
            "memory_job.failed",
            resource_type="memory_outbox",
            resource_id=job["job_id"],
            actor_type=actor,
            details={"status": final},
        )
        return final

    await memory_repo.mark_processing(conn, job["job_id"])
    return "processing"
