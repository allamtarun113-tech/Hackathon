"""Resolve an incident: store the postmortem exactly, then queue it as the canonical memory document.

The postmortem replaces any earlier version under the same Hindsight document ID.
Only feedback the resolver explicitly verifies is promoted into memory; everything
else stays provisional.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.errors import NotFoundError, ValidationFailed
from app.core.redaction import redact_text
from app.db.pool import Connection, Database
from app.db.repositories import audit, conversations, deployments, feedback, incidents, reference
from app.domain.contracts import ResolveIncidentRequest, ResolveIncidentResponse
from app.memory.documents import (
    ChangeEvidence,
    IncidentRecord,
    TimelineAction,
    build_incident_document,
)
from app.observability.tracing import score_trace
from app.services.feedback_service import promote_verified_feedback
from app.services.outbox import enqueue_document


def _clean(values: list[str]) -> list[str]:
    return [redact_text(value).text for value in values if value.strip()]


async def _incident_record(
    conn: Connection, incident_id: str, version: int, engineer_id: str
) -> IncidentRecord:
    incident = await incidents.get_incident(conn, incident_id)
    assert incident is not None
    resolution = incident["resolutions"][0]
    links = await deployments.reviewed_links_for_memory(conn, incident_id)
    engineer_name = await reference.engineer_name(conn, engineer_id) or engineer_id
    team = await reference.escalation_team(conn, incident["service_id"])
    alert = incident["alert"] or {}

    return IncidentRecord(
        incident_id=incident_id,
        title=incident["title"],
        service_id=incident["service_id"],
        team=team,
        severity=incident["severity"],
        environment=incident["environment"],
        started_at=incident["started_at"],
        alert_title=alert.get("title", incident["title"]),
        alert_value=alert.get("value", ""),
        customer_impact=incident.get("customer_impact") or "",
        error_signatures=alert.get("error_signatures", []),
        actions=[
            TimelineAction(
                at=action["performed_at"],
                actor=action["actor_name"] or "unknown",
                action=action["action"],
                outcome=action["outcome"],
                result=action["result"] or "",
                command=action["command"],
            )
            for action in incident["actions"]
        ],
        root_cause=resolution["root_cause"],
        resolution=resolution["resolution"]
        + (f" Commands: {'; '.join(resolution['commands'])}." if resolution["commands"] else "")
        + (
            f" Did not help: {'; '.join(resolution['failed_actions'])}."
            if resolution["failed_actions"]
            else ""
        ),
        resolved_by=engineer_name,
        mttr_minutes=incident["mttr_minutes"],
        follow_ups=[item["description"] for item in incident["follow_ups"]],
        changes=[
            ChangeEvidence(
                link["change_id"],
                link["service_id"],
                link["relationship"],
                link["change_summary"] or link["evidence"] or "",
            )
            for link in links
        ],
        source="live-incident",
        postmortem_version=version,
        supabase_record_id=incident_id,
        original_author=engineer_name,
    )


class ResolutionService:
    """Operator resolution of live incidents."""

    def __init__(self, db: Database, *, bank_id: str) -> None:
        self._db = db
        self._bank_id = bank_id

    async def resolve(self, incident_id: str, body: ResolveIncidentRequest) -> ResolveIncidentResponse:
        resolved_at = body.resolved_at or datetime.now(UTC)
        bank_id = self._bank_id

        async with self._db.transaction() as conn:
            if not await reference.engineer_exists(conn, body.engineer_id):
                raise ValidationFailed("Unknown engineer.")
            incident = await incidents.get_incident(conn, incident_id)
            if incident is None:
                raise NotFoundError("Incident not found.")
            if incident["source"] != "live":
                raise ValidationFailed(
                    "Historical incidents are maintained by the importer, not resolved here."
                )

            verified = await feedback.record_review(
                conn,
                body.verified_feedback_ids,
                incident_id=incident_id,
                state="confirmed_by_resolution",
                reviewer=body.engineer_id,
                note="Confirmed by incident resolution.",
                reviewed_at=resolved_at,
            )
            rejected = await feedback.record_review(
                conn,
                body.rejected_feedback_ids,
                incident_id=incident_id,
                state="rejected",
                reviewer=body.engineer_id,
                note="Rejected at incident resolution.",
                reviewed_at=resolved_at,
            )
            unknown = set(body.verified_feedback_ids + body.rejected_feedback_ids) - {
                row["feedback_id"] for row in [*verified, *rejected]
            }
            if unknown:
                raise ValidationFailed(
                    "Some feedback is not pending feedback on this incident.",
                    details={"feedback_ids": sorted(unknown)},
                )

            # Verified feedback becomes incident actions first, so the postmortem document includes it.
            feedback_jobs = await promote_verified_feedback(conn, verified, bank_id=bank_id)

            version = await incidents.record_resolution(
                conn,
                incident_id=incident_id,
                resolved_by=body.engineer_id,
                root_cause=redact_text(body.root_cause).text,
                resolution=redact_text(body.resolution).text,
                commands=_clean(body.commands),
                failed_actions=_clean(body.failed_actions),
                follow_ups=_clean(body.follow_ups),
                resolved_at=resolved_at,
            )
            record = await _incident_record(conn, incident_id, version, body.engineer_id)
            job = await enqueue_document(
                conn, build_incident_document(record), bank_id=bank_id, actor_id=body.engineer_id
            )

            await conversations.resolve_for_incident(conn, incident_id)
            await audit.record(
                conn,
                "incident.resolved",
                resource_type="incident",
                resource_id=incident_id,
                actor_type="engineer",
                actor_id=body.engineer_id,
                details={
                    "postmortem_version": version,
                    "memory_job_id": job["job_id"],
                    "verified_feedback": [row["feedback_id"] for row in verified],
                    "rejected_feedback": [row["feedback_id"] for row in rejected],
                },
            )
            traces = await conversations.trace_ids_for_incident(conn, incident_id)

        for trace_id in traces:
            score_trace(trace_id, name="incident_resolved", value=1.0, comment=f"postmortem v{version}")

        return ResolveIncidentResponse(
            incident_id=incident_id,
            postmortem_version=version,
            memory_job_id=job["job_id"],
            verified_feedback=len(verified),
            rejected_feedback=len(rejected),
            feedback_memory_job_ids=feedback_jobs,
        )
