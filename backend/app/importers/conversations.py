"""Import conversations, messages, checkpoints and feedback into Supabase.

Conversations are never retained wholesale into Hindsight. Feedback is stored
exactly; only meaningful outcomes from non-evaluation conversations are later
queued for memory by the memory importer.
"""

from __future__ import annotations

from typing import Any

from app.db.pool import Database
from app.db.repositories import conversations, feedback, observability
from app.importers.source import SeedSource, parse_ts

_SOURCE_KIND = {
    "held-out-evaluation": "evaluation-fixture",
    "development-fixture": "development-fixture",
}
_MESSAGE_TYPES = {
    "incident_report": "engineer_input",
    "evidence": "engineer_input",
    "tool_progress": "tool_status",
    "final_answer": "agent_response",
    "followup_question": "agent_response",
    "acknowledgement": "agent_response",
    "resolution_confirmation": "resolution_update",
}
_ROLES = {"user": "engineer", "assistant": "agent", "system": "system"}
_FEEDBACK_OUTCOMES = {"confirmed_fix": "worked"}


async def import_conversations(db: Database, source: SeedSource) -> dict[str, int]:
    convs = source.production("conversations")
    messages_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for message in source.production("messages"):
        messages_by_conversation.setdefault(message["conversation_id"], []).append(message)
    checkpoints = {c["conversation_id"]: c for c in source.production("agent_checkpoints")}

    message_count = 0
    for conv in convs:
        conversation_id = conv["conversation_id"]
        async with db.transaction() as conn:
            await conversations.create_conversation(
                conn,
                conversation_id=conversation_id,
                engineer_id=source.user_to_engineer[conv["user_id"]],
                title=conv["title"],
                service_id=conv.get("service_id"),
                incident_id=conv.get("linked_incident_id"),
                source=_SOURCE_KIND.get(conv["source_kind"], "development-fixture"),
                created_at=parse_ts(conv["created_at"]),
            )
            await conversations.set_status(
                conn, conversation_id, "resolved" if conv["status"] == "resolved" else "active"
            )
            for message in sorted(
                messages_by_conversation.get(conversation_id, []), key=lambda m: m["sequence"]
            ):
                await conversations.add_message(
                    conn,
                    message_id=message["message_id"],
                    conversation_id=conversation_id,
                    role=_ROLES[message["role"]],
                    message_type=_MESSAGE_TYPES.get(message["message_type"], "system_event"),
                    content=message["content"],
                    engineer_id=source.user_to_engineer.get(message.get("user_id") or ""),
                    redaction_status="passed" if message.get("redaction_status") == "passed" else "redacted",
                    created_at=parse_ts(message["created_at"]),
                )
                message_count += 1

            checkpoint = checkpoints.get(conversation_id)
            if checkpoint:
                state = checkpoint["state"]
                await conversations.upsert_checkpoint(
                    conn,
                    conversation_id,
                    {
                        "incident_id": conv.get("linked_incident_id"),
                        "service_id": state.get("service"),
                        "symptoms": (conv.get("expected_extraction") or {}).get("symptoms"),
                        "error_signatures": [state["signature"]] if state.get("signature") else [],
                        "matched_incidents": [conv["linked_incident_id"]]
                        if conv.get("linked_incident_id")
                        else [],
                        "graph_node": checkpoint["last_node"],
                        "step_count": checkpoint["step_count"],
                        "updated_at": parse_ts(checkpoint["created_at"]),
                    },
                )
    return {"conversations": len(convs), "messages": message_count, "agent_checkpoints": len(checkpoints)}


async def import_feedback(db: Database, source: SeedSource) -> dict[str, int]:
    rows = source.production("feedback")
    services = {c["conversation_id"]: c.get("service_id") for c in source.production("conversations")}
    async with db.transaction() as conn:
        for row in rows:
            outcome = _FEEDBACK_OUTCOMES.get(row["outcome"], row["outcome"])
            await feedback.insert_feedback(
                conn,
                {
                    "feedback_id": row["feedback_id"],
                    "conversation_id": row["conversation_id"],
                    "message_id": row.get("message_id"),
                    "incident_id": row.get("incident_id"),
                    "step_id": row.get("step_id"),
                    "engineer_id": source.user_to_engineer.get(row["user_id"]),
                    "service_id": services.get(row["conversation_id"]),
                    "rating": row["rating"] if row.get("rating") in (-1, 1) else None,
                    "outcome": outcome,
                    "note": row.get("note"),
                    "validation_state": "pending",
                    "retain_status": "not_eligible",
                    "created_at": parse_ts(row["created_at"]),
                },
            )
        # Mark evaluation-derived feedback so it can never be promoted into memory.
        await feedback.reject_evaluation_fixture_feedback(conn)
    return {"feedback": len(rows)}


async def import_observability_fixtures(db: Database, source: SeedSource) -> dict[str, int]:
    """Seeded dashboard examples, explicitly labelled as synthetic fixtures."""
    snapshots = source.production("health_snapshots")
    async with db.transaction() as conn:
        for snapshot in snapshots:
            await observability.insert_fixture_snapshot(
                conn,
                snapshot_id=snapshot["health_snapshot_id"],
                overall_status=snapshot["overall_status"],
                dependencies=snapshot["dependencies"],
                checked_at=parse_ts(snapshot["checked_at"]),
            )
    return {"health_snapshots": len(snapshots)}
