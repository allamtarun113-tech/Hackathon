"""Append-only, hash-chained audit log (the chain itself is maintained by a database trigger)."""

from __future__ import annotations

from typing import Any, Literal

from app.core.context import current_request_id
from app.db.json import jsonb
from app.db.pool import Connection

ActorType = Literal["engineer", "system", "worker", "webhook", "ci"]


async def record(
    conn: Connection,
    event_type: str,
    *,
    resource_type: str,
    resource_id: str | None,
    actor_type: ActorType = "system",
    actor_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    # previous_hash / event_hash are set by the audit_events_chain trigger.
    await conn.execute(
        """
        insert into audit_events (event_type, actor_type, actor_id, resource_type, resource_id, request_id,
                                  details, previous_hash, event_hash)
        values (%s, %s, %s, %s, %s, %s, %s, '', '')
        """,
        (
            event_type,
            actor_type,
            actor_id,
            resource_type,
            resource_id,
            current_request_id(),
            jsonb(details or {}),
        ),
    )
