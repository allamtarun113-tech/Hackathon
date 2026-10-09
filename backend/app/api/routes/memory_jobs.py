"""GET /memory-jobs/{job_id} - public progress view; operators also see error details.
GET /memory-jobs?status=needs_review - operator queue of retains that produced no searchable memory."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from app.api.deps import ContainerDep
from app.api.security import OperatorOnly, is_operator
from app.core.errors import NotFoundError
from app.db.repositories import memory
from app.domain.contracts import ID_PATTERN, MemoryJobDetail, MemoryJobResponse

router = APIRouter(tags=["memory"])

AttentionStatus = Literal["needs_review", "dead_letter", "rejected"]


@router.get("/memory-jobs", response_model=list[MemoryJobDetail])
async def list_memory_jobs(
    container: ContainerDep,
    _: OperatorOnly,
    status: Annotated[list[AttentionStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[MemoryJobDetail]:
    """Operator only: jobs that ended without searchable memory (zero units, guardrail rejection, dead letter)."""
    async with container.db.transaction() as conn:
        jobs = await memory.jobs_needing_attention(
            conn, statuses=tuple(status) if status else memory.ATTENTION_STATES, limit=limit
        )
    return [MemoryJobDetail.model_validate(job) for job in jobs]


@router.get("/memory-jobs/{job_id}", response_model=MemoryJobDetail | MemoryJobResponse)
async def get_memory_job(
    request: Request, container: ContainerDep, job_id: str = Path(pattern=ID_PATTERN)
) -> MemoryJobDetail | MemoryJobResponse:
    async with container.db.transaction() as conn:
        job = await memory.get_job(conn, job_id)
    if job is None:
        raise NotFoundError("Memory job not found.")
    if is_operator(request, container.settings):
        return MemoryJobDetail.model_validate(job)
    return MemoryJobResponse.model_validate(job)
