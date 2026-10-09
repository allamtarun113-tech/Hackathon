"""POST /chat - streams agent progress and the final answer as Server-Sent Events."""

from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api.deps import ContainerDep
from app.core.errors import DependencyUnavailable
from app.domain.contracts import ChatRequest

router = APIRouter(tags=["chat"])


@router.post("/chat")
async def chat(body: ChatRequest, container: ContainerDep) -> EventSourceResponse:
    """Event types: `conversation`, `progress`, `baseline`, `recommendation`, `clarification`, `rejected`,
    `error`, `done`. Every event payload is JSON."""
    if container.agent.at_capacity:
        raise DependencyUnavailable(
            "PagerMind is handling the maximum number of incidents. Retry in a moment."
        )
    events = container.agent.stream(body)
    return EventSourceResponse(
        events, ping=15, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )
