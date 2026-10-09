"""Builds the Hindsight retain items for every memory type.

Each builder returns a `MemoryDocument` with a stable document ID so that
re-imports and postmortem revisions replace the existing memory instead of
duplicating it. All content passes through redaction before it leaves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.core.redaction import redact_text
from app.memory.bank import incident_tags, memory_tags

SCHEMA_VERSION = "2.0"

OUTCOME_LABELS = {
    "worked": "WORKED",
    "partial": "PARTLY HELPED",
    "did_not_work": "DID NOT WORK",
    "made_worse": "MADE IT WORSE",
}

SourceType = Literal["incident", "resolution", "feedback", "runbook"]
MemoryType = Literal["incident", "procedure", "feedback", "live_step"]


@dataclass(frozen=True)
class TimelineAction:
    at: datetime
    actor: str
    action: str
    outcome: str
    result: str
    command: str | None = None


@dataclass(frozen=True)
class ChangeEvidence:
    change_id: str
    service_id: str
    relationship: str
    summary: str


@dataclass(frozen=True)
class IncidentRecord:
    """Normalised incident, independent of whether it came from the seed JSON or Supabase."""

    incident_id: str
    title: str
    service_id: str
    team: str | None
    severity: str
    environment: str
    started_at: datetime
    alert_title: str
    alert_value: str
    customer_impact: str
    error_signatures: list[str]
    actions: list[TimelineAction]
    root_cause: str
    resolution: str
    resolved_by: str
    mttr_minutes: int | None
    follow_ups: list[str]
    changes: list[ChangeEvidence] = field(default_factory=list)
    source: Literal["historical-seed", "live-incident"] = "historical-seed"
    postmortem_version: int = 1
    supabase_record_id: str | None = None
    original_author: str | None = None


@dataclass(frozen=True)
class MemoryDocument:
    document_id: str
    memory_type: MemoryType
    source_type: SourceType
    source_id: str
    update_mode: Literal["replace", "append"]
    item: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        """JSON-serialisable form stored in the outbox (and checksummed)."""
        item = dict(self.item)
        if isinstance(item.get("timestamp"), datetime):
            item["timestamp"] = item["timestamp"].isoformat()
        return {"document_id": self.document_id, "update_mode": self.update_mode, "item": item}


def incident_document_id(incident_id: str) -> str:
    return f"incident:{incident_id}"


def _clean(text: str) -> str:
    return redact_text(text).text


ENTITY_TEXT_LIMIT = 200


def entity(kind: str, text: str) -> dict[str, str]:
    """An entity Hindsight attaches to every fact extracted from the item, in addition to what it extracts."""
    return {"text": _clean(text.strip())[:ENTITY_TEXT_LIMIT], "type": kind}


def incident_entities(
    *, incident_id: str, service_id: str, error_signatures: list[str]
) -> list[dict[str, str]]:
    entities = [entity("incident", incident_id), entity("service", service_id)]
    entities += [
        entity("error_signature", signature) for signature in dict.fromkeys(error_signatures) if signature
    ]
    return entities


def build_incident_document(record: IncidentRecord) -> MemoryDocument:
    """The richest approved representation of one incident: one stable document per incident."""
    timeline = "\n".join(
        f"  {action.at:%Y-%m-%d %H:%M} {action.actor}: {action.action}"
        + (f" -> {OUTCOME_LABELS[action.outcome]}" if action.outcome in OUTCOME_LABELS else "")
        + (f". {action.result}" if action.result else "")
        + (f" (command: {action.command})" if action.command else "")
        for action in record.actions
    )
    attempted = [a for a in record.actions if a.outcome in OUTCOME_LABELS]
    failed = [f"{a.action} ({a.result})" for a in attempted if a.outcome in ("did_not_work", "made_worse")]
    commands = sorted({a.command for a in record.actions if a.command})
    signatures = "\n".join(f"  {signature}" for signature in record.error_signatures) or "  (none captured)"
    changes = (
        "\n".join(
            f"  {change.change_id} on {change.service_id}: {change.relationship.replace('_', ' ')} - {change.summary}"
            for change in record.changes
        )
        or "  (no reviewed change links)"
    )

    content = (
        f"Incident {record.incident_id}: {record.title}\n"
        f"Timestamp: {record.started_at:%Y-%m-%d %H:%M %Z} ({record.started_at:%A}). "
        f"Service: {record.service_id}. Environment: {record.environment}. Severity: {record.severity}.\n"
        f"Alert: {record.alert_title} ({record.alert_value}).\n"
        f"Customer impact: {record.customer_impact}\n"
        f"Exact error signatures:\n{signatures}\n"
        f"Deployments investigated:\n{changes}\n"
        f"Timeline:\n{timeline}\n"
        f"Actions that did NOT work: {'; '.join(failed) or 'none recorded'}.\n"
        f"Root cause: {record.root_cause}\n"
        f"Resolution: {record.resolution}\n"
        f"Commands used: {'; '.join(commands) or 'none recorded'}.\n"
        f"Resolved by {record.resolved_by}"
        + (f" in {record.mttr_minutes} minutes" if record.mttr_minutes is not None else "")
        + f". Follow-ups: {'; '.join(record.follow_ups) or 'none'}."
    )

    item = {
        "content": _clean(content),
        "timestamp": record.started_at,
        "context": f"Resolved incident postmortem for {record.service_id} (v{record.postmortem_version})",
        "metadata": {
            "incident_id": record.incident_id,
            "supabase_record_id": record.supabase_record_id or record.incident_id,
            "service": record.service_id,
            "title": record.title,
            "source_type": record.source,
            "schema_version": SCHEMA_VERSION,
            "review_status": "approved",
            "original_author": record.original_author or record.resolved_by,
            "postmortem_version": str(record.postmortem_version),
        },
        "tags": incident_tags(
            service=record.service_id,
            team=record.team,
            severity=record.severity,
            environment=record.environment,
            source=record.source,
        ),
        "entities": incident_entities(
            incident_id=record.incident_id,
            service_id=record.service_id,
            error_signatures=record.error_signatures,
        ),
        "update_mode": "replace",
    }
    return MemoryDocument(
        document_id=incident_document_id(record.incident_id),
        memory_type="incident",
        source_type="resolution" if record.source == "live-incident" else "incident",
        source_id=record.incident_id,
        update_mode="replace",
        item=item,
    )


def build_feedback_document(
    *,
    feedback_id: str,
    incident_id: str,
    service_id: str,
    engineer_name: str,
    step_action: str,
    outcome: str,
    note: str,
    occurred_at: datetime,
) -> MemoryDocument:
    """A verified outcome of one recommended step. Appended to the incident's feedback document."""
    content = (
        f"During incident {incident_id} on {service_id}, {engineer_name} tried the recommended step "
        f"'{step_action}'. Outcome: {OUTCOME_LABELS.get(outcome, outcome)}. {note}".strip()
    )
    item = {
        "content": _clean(content),
        "timestamp": occurred_at,
        "context": f"Verified feedback on a recommended step during a live {service_id} incident",
        "metadata": {
            "incident_id": incident_id,
            "supabase_record_id": feedback_id,
            "service": service_id,
            "source_type": "live-incident",
            "schema_version": SCHEMA_VERSION,
            "review_status": "confirmed",
            "original_author": engineer_name,
        },
        "tags": memory_tags(
            memory_type="feedback", service=service_id, outcome=outcome, source="live-incident"
        ),
        "entities": [
            entity("incident", incident_id),
            entity("service", service_id),
            entity("action", step_action),
        ],
        "update_mode": "append",
    }
    return MemoryDocument(
        document_id=f"feedback:{incident_id}",
        memory_type="feedback",
        source_type="feedback",
        source_id=feedback_id,
        update_mode="append",
        item=item,
    )


def build_runbook_document(
    *,
    runbook_id: str,
    service_id: str | None,
    title: str,
    version: int,
    markdown: str,
    approved_at: datetime,
) -> MemoryDocument:
    """Approved procedures only. Drafts and superseded versions are never retained."""
    item = {
        "content": _clean(f"Approved runbook '{title}' (version {version}).\n\n{markdown}"),
        "timestamp": approved_at,
        "context": f"Approved operational procedure for {service_id or 'all services'}",
        "metadata": {
            "runbook_id": runbook_id,
            "service": service_id or "all",
            "title": title,
            "source_type": "runbook",
            "schema_version": SCHEMA_VERSION,
            "review_status": "approved",
            "postmortem_version": str(version),
        },
        # Same production scope as incidents, so approved procedures are reachable by reflect.
        "tags": memory_tags(memory_type="procedure", service=service_id or "all", source="runbook"),
        "entities": [entity("service", service_id)] if service_id else [],
        "update_mode": "replace",
    }
    return MemoryDocument(
        document_id=f"runbook:{runbook_id}",
        memory_type="procedure",
        source_type="runbook",
        source_id=runbook_id,
        update_mode="replace",
        item=item,
    )
