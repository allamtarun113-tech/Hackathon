"""GET /incidents/{incident_id} and POST /incidents/{incident_id}/resolve."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path

from app.api.deps import ContainerDep
from app.api.security import OperatorOnly
from app.core.errors import NotFoundError
from app.db.repositories import feedback, incidents, memory
from app.domain.contracts import ID_PATTERN, ResolveIncidentRequest, ResolveIncidentResponse

router = APIRouter(tags=["incidents"])


@router.get("/incidents/{incident_id}")
async def get_incident(
    container: ContainerDep, _: OperatorOnly, incident_id: str = Path(pattern=ID_PATTERN)
) -> dict[str, Any]:
    """Operator only: includes provisional feedback and memory status."""
    async with container.db.transaction() as conn:
        incident = await incidents.get_incident(conn, incident_id)
        if incident is None:
            raise NotFoundError("Incident not found.")
        incident["feedback"] = await feedback.feedback_for_incident(conn, incident_id)
        incident["memory_documents"] = await memory.documents_for_source(conn, incident_id)
    return incident


@router.post("/incidents/{incident_id}/resolve", response_model=ResolveIncidentResponse)
async def resolve(
    body: ResolveIncidentRequest,
    container: ContainerDep,
    _: OperatorOnly,
    incident_id: str = Path(pattern=ID_PATTERN),
) -> ResolveIncidentResponse:
    """Operator only: replaces canonical root cause / resolution and queues the postmortem for memory."""
    return await container.resolutions.resolve(incident_id, body)
