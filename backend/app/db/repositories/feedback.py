"""Feedback on agent recommendations.

Feedback is provisional (`pending`) when submitted. It only becomes eligible for
memory once it is verified by an operator review or confirmed by the incident
resolution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db.pool import Connection


async def insert_feedback(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into feedback (
            feedback_id, conversation_id, message_id, incident_id, step_id, engineer_id, service_id, rating,
            outcome, note, validation_state, retain_status, created_at
        ) values (
            %(feedback_id)s, %(conversation_id)s, %(message_id)s, %(incident_id)s, %(step_id)s, %(engineer_id)s,
            %(service_id)s, %(rating)s, %(outcome)s, %(note)s, %(validation_state)s, %(retain_status)s,
            coalesce(%(created_at)s, now())
        )
        on conflict (feedback_id) do nothing
        """,
        {"created_at": None, **row},
    )


async def set_retain_status(conn: Connection, feedback_id: str, *, retain_status: str) -> None:
    await conn.execute(
        "update feedback set retain_status = %s where feedback_id = %s",
        (retain_status, feedback_id),
    )


async def get_feedback(conn: Connection, feedback_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute(
        """
        select f.*, e.display_name as engineer_name, c.source as conversation_source
        from feedback f
        left join engineers e using (engineer_id)
        join conversations c using (conversation_id)
        where f.feedback_id = %s
        for update of f
        """,
        (feedback_id,),
    )
    return await cursor.fetchone()


async def feedback_for_incident(conn: Connection, incident_id: str) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select f.*, e.display_name as engineer_name
        from feedback f
        left join engineers e using (engineer_id)
        where f.incident_id = %s
        order by f.created_at
        """,
        (incident_id,),
    )
    return await cursor.fetchall()


async def record_review(
    conn: Connection,
    feedback_ids: list[str],
    *,
    incident_id: str | None,
    state: str,
    reviewer: str,
    note: str | None,
    reviewed_at: datetime,
) -> list[dict[str, Any]]:
    """Move pending feedback to a reviewed state. Returns the rows that actually changed.

    Only pending feedback on live conversations can be reviewed, and when `incident_id`
    is given every row must belong to that incident.
    """
    if not feedback_ids:
        return []
    cursor = await conn.execute(
        """
        update feedback f
        set validation_state = %s, verified_by = %s, verified_at = %s, review_note = %s
        from conversations c
        where c.conversation_id = f.conversation_id
          and c.source = 'live'
          and f.feedback_id = any(%s)
          and f.validation_state = 'pending'
          and (%s::text is null or f.incident_id = %s::text)
        returning f.*
        """,
        (state, reviewer, reviewed_at, note, feedback_ids, incident_id, incident_id),
    )
    return await cursor.fetchall()


async def reject_evaluation_fixture_feedback(conn: Connection) -> int:
    """Feedback on held-out evaluation conversations can never be promoted into memory."""
    cursor = await conn.execute(
        """
        update feedback f set validation_state = 'rejected'
        from conversations c
        where c.conversation_id = f.conversation_id and c.source = 'evaluation-fixture'
          and f.validation_state <> 'rejected'
        """
    )
    return cursor.rowcount
