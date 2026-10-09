"""Conversations, messages, checkpoints, agent runs and tool calls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db.json import jsonb
from app.db.pool import Connection


async def create_conversation(
    conn: Connection,
    *,
    conversation_id: str,
    engineer_id: str,
    title: str,
    service_id: str | None = None,
    incident_id: str | None = None,
    source: str = "live",
    created_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        insert into conversations (conversation_id, engineer_id, service_id, incident_id, title, source, created_at)
        values (%s, %s, %s, %s, %s, %s, coalesce(%s, now()))
        on conflict (conversation_id) do nothing
        """,
        (conversation_id, engineer_id, service_id, incident_id, title, source, created_at),
    )


async def get_conversation(conn: Connection, conversation_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute("select * from conversations where conversation_id = %s", (conversation_id,))
    conversation = await cursor.fetchone()
    if conversation is None:
        return None
    messages = await conn.execute(
        """
        select message_id, sequence, role, message_type, engineer_id, content, payload, created_at
        from messages where conversation_id = %s order by sequence
        """,
        (conversation_id,),
    )
    checkpoint = await conn.execute(
        "select * from agent_checkpoints where conversation_id = %s", (conversation_id,)
    )
    conversation["messages"] = await messages.fetchall()
    conversation["checkpoint"] = await checkpoint.fetchone()
    return conversation


async def attach_context(
    conn: Connection, conversation_id: str, *, service_id: str | None, incident_id: str | None
) -> None:
    await conn.execute(
        """
        update conversations
        set service_id = coalesce(service_id, %s),
            incident_id = coalesce(incident_id, %s)
        where conversation_id = %s
        """,
        (service_id, incident_id, conversation_id),
    )


async def set_status(conn: Connection, conversation_id: str, status: str) -> None:
    await conn.execute(
        "update conversations set status = %s where conversation_id = %s", (status, conversation_id)
    )


async def add_message(
    conn: Connection,
    *,
    message_id: str,
    conversation_id: str,
    role: str,
    message_type: str,
    content: str,
    engineer_id: str | None = None,
    payload: dict[str, Any] | None = None,
    redaction_status: str = "passed",
    request_id: str | None = None,
    created_at: datetime | None = None,
) -> int:
    """Append a message with the next sequence number. The conversation row is locked to serialise writers."""
    await conn.execute(
        "select 1 from conversations where conversation_id = %s for update", (conversation_id,)
    )
    cursor = await conn.execute(
        "select coalesce(max(sequence), 0) + 1 as next from messages where conversation_id = %s",
        (conversation_id,),
    )
    sequence: int = (await cursor.fetchone() or {"next": 1})["next"]
    await conn.execute(
        """
        insert into messages (message_id, conversation_id, sequence, role, message_type, engineer_id, content,
                              payload, redaction_status, request_id, created_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))
        on conflict (message_id) do nothing
        """,
        (
            message_id,
            conversation_id,
            sequence,
            role,
            message_type,
            engineer_id,
            content,
            jsonb(payload) if payload is not None else None,
            redaction_status,
            request_id,
            created_at,
        ),
    )
    return sequence


async def get_message(conn: Connection, message_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute("select * from messages where message_id = %s", (message_id,))
    return await cursor.fetchone()


async def upsert_checkpoint(conn: Connection, conversation_id: str, state: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into agent_checkpoints (
            conversation_id, incident_id, service_id, symptoms, error_signatures, recent_changes,
            matched_incidents, graph_node, step_count, recommendation, last_error, updated_at
        ) values (
            %(conversation_id)s, %(incident_id)s, %(service_id)s, %(symptoms)s, %(error_signatures)s,
            %(recent_changes)s, %(matched_incidents)s, %(graph_node)s, %(step_count)s, %(recommendation)s,
            %(last_error)s, %(updated_at)s
        )
        on conflict (conversation_id) do update set
            incident_id = excluded.incident_id,
            service_id = excluded.service_id,
            symptoms = excluded.symptoms,
            error_signatures = excluded.error_signatures,
            recent_changes = excluded.recent_changes,
            matched_incidents = excluded.matched_incidents,
            graph_node = excluded.graph_node,
            step_count = excluded.step_count,
            recommendation = excluded.recommendation,
            last_error = excluded.last_error,
            version = agent_checkpoints.version + 1,
            updated_at = excluded.updated_at
        """,
        {
            "conversation_id": conversation_id,
            "incident_id": state.get("incident_id"),
            "service_id": state.get("service_id"),
            "symptoms": state.get("symptoms"),
            "error_signatures": state.get("error_signatures", []),
            "recent_changes": jsonb(state.get("recent_changes", [])),
            "matched_incidents": state.get("matched_incidents", []),
            "graph_node": state["graph_node"],
            "step_count": state.get("step_count", 0),
            "recommendation": jsonb(state["recommendation"]) if state.get("recommendation") else None,
            "last_error": state.get("last_error"),
            "updated_at": state.get("updated_at") or datetime.now(UTC),
        },
    )


async def start_run(conn: Connection, *, run_id: str, conversation_id: str, request_id: str) -> None:
    await conn.execute(
        "insert into agent_runs (run_id, conversation_id, request_id, status) values (%s, %s, %s, 'running')",
        (run_id, conversation_id, request_id),
    )


async def finish_run(conn: Connection, run_id: str, result: dict[str, Any]) -> None:
    await conn.execute(
        """
        update agent_runs set
            message_id = %(message_id)s,
            status = %(status)s,
            outcome = %(outcome)s,
            match_strength = %(match_strength)s,
            memory_available = %(memory_available)s,
            model = %(model)s,
            fallback_used = %(fallback_used)s,
            input_tokens = %(input_tokens)s,
            output_tokens = %(output_tokens)s,
            latency_ms = %(latency_ms)s,
            error = %(error)s,
            finished_at = now()
        where run_id = %(run_id)s
        """,
        {"run_id": run_id, **result},
    )


async def add_tool_calls(conn: Connection, run_id: str, calls: list[dict[str, Any]]) -> None:
    if not calls:
        return
    async with conn.cursor() as cursor:
        await cursor.executemany(
            """
            insert into tool_calls (run_id, node, tool, status, latency_ms, input_summary, output_summary, error)
            values (%(run_id)s, %(node)s, %(tool)s, %(status)s, %(latency_ms)s, %(input_summary)s,
                    %(output_summary)s, %(error)s)
            """,
            [
                {"input_summary": None, "output_summary": None, "error": None, **call, "run_id": run_id}
                for call in calls
            ],
        )


async def link_trace(
    conn: Connection,
    *,
    request_id: str,
    conversation_id: str,
    run_id: str,
    langfuse_trace_id: str | None,
) -> None:
    await conn.execute(
        """
        insert into trace_links (request_id, conversation_id, run_id, langfuse_trace_id)
        values (%s, %s, %s, %s)
        on conflict (request_id) do update set langfuse_trace_id = excluded.langfuse_trace_id
        """,
        (request_id, conversation_id, run_id, langfuse_trace_id),
    )


async def resolve_for_incident(conn: Connection, incident_id: str) -> None:
    await conn.execute("update conversations set status = 'resolved' where incident_id = %s", (incident_id,))


async def trace_ids_for_incident(conn: Connection, incident_id: str) -> list[str]:
    cursor = await conn.execute(
        """
        select t.langfuse_trace_id from trace_links t
        join conversations c using (conversation_id)
        where c.incident_id = %s and t.langfuse_trace_id is not null
        """,
        (incident_id,),
    )
    return [row["langfuse_trace_id"] for row in await cursor.fetchall()]


async def trace_id_for_request(conn: Connection, request_id: str | None) -> str | None:
    if not request_id:
        return None
    cursor = await conn.execute(
        "select langfuse_trace_id from trace_links where request_id = %s", (request_id,)
    )
    row = await cursor.fetchone()
    return row["langfuse_trace_id"] if row else None
