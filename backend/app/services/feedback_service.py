"""Engineer feedback.

Submitting feedback never writes memory. It is stored as provisional and only
promoted into Hindsight after an operator verifies it (review endpoint) or the
incident resolution confirms it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.context import new_id
from app.core.errors import NotFoundError, ValidationFailed
from app.core.redaction import redact_text
from app.db.pool import Connection, Database
from app.db.repositories import audit, conversations, feedback, incidents, reference
from app.domain.contracts import (
    FeedbackRequest,
    FeedbackResponse,
    FeedbackReviewRequest,
    FeedbackReviewResponse,
)
from app.guardrails.memory_guard import is_meaningful_feedback
from app.memory.documents import build_feedback_document
from app.observability.tracing import score_trace
from app.services.outbox import enqueue_document

_OUTCOME_SCORES = {"worked": 1.0, "partial": 0.5, "did_not_work": 0.0, "made_worse": -1.0}


def _step_action(message: dict[str, Any] | None, step_id: str | None) -> str | None:
    recommendation = ((message or {}).get("payload") or {}).get("recommendation") or {}
    for step in recommendation.get("recommended_steps", []):
        if step.get("step_id") == step_id:
            return str(step.get("action"))
    return None


async def promote_verified_feedback(
    conn: Connection, rows: list[dict[str, Any]], *, bank_id: str
) -> list[str]:
    """Turn verified feedback into exact incident actions and queued memory documents."""
    job_ids: list[str] = []
    for row in rows:
        verdict = is_meaningful_feedback(outcome=row["outcome"], note=row["note"], confirmed=True)
        message = await conversations.get_message(conn, row["message_id"]) if row["message_id"] else None
        action = _step_action(message, row["step_id"])
        if not verdict.allowed or action is None or row["incident_id"] is None:
            continue

        incident = await incidents.get_incident(conn, row["incident_id"])
        if incident is None:
            continue
        engineer_name = await _engineer_name(conn, row["engineer_id"])
        await incidents.add_action(
            conn,
            incident_id=row["incident_id"],
            actor_name=engineer_name,
            action=action,
            command=None,
            outcome=row["outcome"],
            result=row["note"],
            performed_at=row["created_at"],
        )
        document = build_feedback_document(
            feedback_id=row["feedback_id"],
            incident_id=row["incident_id"],
            service_id=incident["service_id"],
            engineer_name=engineer_name,
            step_action=action,
            outcome=row["outcome"],
            note=row["note"] or "",
            occurred_at=row["created_at"],
        )
        job = await enqueue_document(conn, document, bank_id=bank_id)
        await feedback.set_retain_status(conn, row["feedback_id"], retain_status="queued")
        job_ids.append(job["job_id"])
    return job_ids


async def _engineer_name(conn: Connection, engineer_id: str | None) -> str:
    return await reference.engineer_name(conn, engineer_id) or engineer_id or "an engineer"


class FeedbackService:
    """Provisional feedback capture and operator review."""

    def __init__(self, db: Database, *, bank_id: str) -> None:
        self._db = db
        self._bank_id = bank_id

    async def submit(self, body: FeedbackRequest) -> FeedbackResponse:
        note = redact_text(body.note).text if body.note else None
        feedback_id = new_id("FDB")

        async with self._db.transaction() as conn:
            if not await reference.engineer_exists(conn, body.engineer_id):
                raise ValidationFailed("Unknown engineer.")
            conversation = await conversations.get_conversation(conn, body.conversation_id)
            message = await conversations.get_message(conn, body.message_id)
            if conversation is None or message is None or message["conversation_id"] != body.conversation_id:
                raise NotFoundError("Conversation or message not found.")
            if message["message_type"] != "agent_response":
                raise ValidationFailed("Feedback can only be given on agent responses.")
            if body.step_id and _step_action(message, body.step_id) is None:
                raise ValidationFailed("Unknown recommended step.")

            # Could this become memory once verified? (A bare rating never can.)
            candidate = (
                body.outcome not in (None, "not_tried")
                and conversation["source"] == "live"
                and conversation["incident_id"] is not None
                and body.step_id is not None
            )
            await feedback.insert_feedback(
                conn,
                {
                    "feedback_id": feedback_id,
                    "conversation_id": body.conversation_id,
                    "message_id": body.message_id,
                    "incident_id": conversation["incident_id"],
                    "step_id": body.step_id,
                    "engineer_id": body.engineer_id,
                    "service_id": conversation["service_id"],
                    "rating": body.rating,
                    "outcome": body.outcome,
                    "note": note,
                    "validation_state": "pending",
                    "retain_status": "not_eligible",
                },
            )
            await audit.record(
                conn,
                "feedback.saved",
                resource_type="feedback",
                resource_id=feedback_id,
                actor_type="engineer",
                actor_id=body.engineer_id,
                details={"outcome": body.outcome, "rating": body.rating, "provisional": True},
            )
            trace = await conversations.trace_id_for_request(conn, message.get("request_id"))

        if trace:
            if body.rating is not None:
                score_trace(trace, name="engineer_rating", value=float(body.rating))
            if body.outcome in _OUTCOME_SCORES:
                score_trace(
                    trace, name="step_outcome", value=_OUTCOME_SCORES[body.outcome], comment=body.step_id
                )

        return FeedbackResponse(
            feedback_id=feedback_id,
            validation_state="pending",
            retain_status="provisional" if candidate else "not_eligible",
            reason=(
                "Saved as provisional. It is remembered only after the incident resolution or an operator verifies it."
                if candidate
                else "Saved. Ratings without an outcome on a recommended step are not remembered."
            ),
        )

    async def review(self, feedback_id: str, body: FeedbackReviewRequest) -> FeedbackReviewResponse:
        """Operator verification (or rejection) of one provisional feedback item."""
        async with self._db.transaction() as conn:
            row = await feedback.get_feedback(conn, feedback_id)
            if row is None:
                raise NotFoundError("Feedback not found.")
            if row["validation_state"] != "pending":
                raise ValidationFailed(f"Feedback is already {row['validation_state']}.")
            reviewed = await feedback.record_review(
                conn,
                [feedback_id],
                incident_id=None,
                state="verified" if body.decision == "verify" else "rejected",
                reviewer=body.reviewer,
                note=redact_text(body.note).text if body.note else None,
                reviewed_at=datetime.now(UTC),
            )
            if not reviewed:
                raise ValidationFailed("Only feedback from live conversations can be reviewed.")
            job_ids = (
                await promote_verified_feedback(conn, reviewed, bank_id=self._bank_id)
                if body.decision == "verify"
                else []
            )
            await audit.record(
                conn,
                "feedback.reviewed",
                resource_type="feedback",
                resource_id=feedback_id,
                actor_type="engineer",
                actor_id=body.reviewer,
                details={"decision": body.decision, "memory_job_ids": job_ids},
            )
        return FeedbackReviewResponse(
            feedback_id=feedback_id,
            validation_state="verified" if body.decision == "verify" else "rejected",
            memory_job_id=job_ids[0] if job_ids else None,
        )
