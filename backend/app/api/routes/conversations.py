"""GET /conversations/{conversation_id}."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Path

from app.api.deps import ContainerDep
from app.api.security import OperatorOnly
from app.core.errors import NotFoundError
from app.db.repositories import conversations
from app.domain.contracts import ID_PATTERN

router = APIRouter(tags=["conversations"])


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    container: ContainerDep,
    _: OperatorOnly,
    conversation_id: str = Path(pattern=ID_PATTERN),
) -> dict[str, Any]:
    """Operator only: conversations contain internal checkpoints and agent payloads."""
    async with container.db.transaction() as conn:
        conversation = await conversations.get_conversation(conn, conversation_id)
    if conversation is None:
        raise NotFoundError("Conversation not found.")
    return conversation
