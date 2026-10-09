"""Queue historical knowledge for Hindsight via the outbox.

Nothing is sent to Hindsight here: rows are written to `memory_outbox` and the
memory worker submits them. Documents whose content has not changed since the
last successful retain are skipped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.hashing import payload_checksum
from app.db.pool import Database
from app.db.repositories import deployments, files, memory
from app.importers.source import SeedSource, parse_ts
from app.memory.documents import (
    ChangeEvidence,
    IncidentRecord,
    MemoryDocument,
    TimelineAction,
    build_incident_document,
    build_runbook_document,
)
from app.memory.signatures import error_signatures
from app.services.outbox import enqueue_document

_SKIPPED_TIMELINE_ACTIONS = {"Acknowledged the page", "Marked the incident resolved"}


def incident_record_from_seed(
    incident: dict[str, Any], team: str | None, changes: list[ChangeEvidence]
) -> IncidentRecord:
    started_at = parse_ts(incident["date"])
    assert started_at is not None
    return IncidentRecord(
        incident_id=incident["id"],
        title=incident["title"],
        service_id=incident["service"],
        team=team,
        severity=incident["severity"],
        environment=incident.get("environment", "production"),
        started_at=started_at,
        alert_title=incident["alert"]["title"],
        alert_value=incident["alert"].get("value", ""),
        customer_impact=incident.get("customer_impact", ""),
        error_signatures=error_signatures(incident.get("log_excerpt", [])),
        actions=[
            TimelineAction(
                at=datetime.fromisoformat(step["at"]),
                actor=step["actor"],
                action=step["action"],
                outcome=step["outcome"],
                result=step.get("result", ""),
                command=step.get("command"),
            )
            for step in incident["timeline"]
            if step["action"] not in _SKIPPED_TIMELINE_ACTIONS
        ],
        root_cause=incident["root_cause"],
        resolution=incident["resolution"],
        resolved_by=incident["resolved_by"],
        mttr_minutes=incident.get("mttr_minutes"),
        follow_ups=incident.get("action_items", []),
        changes=changes,
        source="historical-seed",
        original_author=incident["resolved_by"],
    )


async def _enqueue_if_changed(db: Database, document: MemoryDocument, bank_id: str) -> bool:
    async with db.transaction() as conn:
        current = await memory.document_checksum(conn, document.document_id)
        if current == payload_checksum(document.payload()):
            return False
        result = await enqueue_document(conn, document, bank_id=bank_id)
        return bool(result["created"])


async def queue_incident_memories(db: Database, source: SeedSource, bank_id: str) -> dict[str, int]:
    teams = {service_id: service["team"] for service_id, service in source.catalog["services"].items()}
    queued = 0
    for incident in source.incidents:
        async with db.transaction() as conn:
            links = await deployments.reviewed_links_for_memory(conn, incident["id"])
        changes = [
            ChangeEvidence(
                change_id=link["change_id"],
                service_id=link["service_id"],
                relationship=link["relationship"],
                summary=link["change_summary"] or link["evidence"] or "",
            )
            for link in links
        ]
        record = incident_record_from_seed(incident, teams.get(incident["service"]), changes)
        queued += await _enqueue_if_changed(db, build_incident_document(record), bank_id)
    return {"incident_documents_queued": queued, "incident_documents_total": len(source.incidents)}


async def queue_runbook_memories(db: Database, source: SeedSource, bank_id: str) -> dict[str, int]:
    queued = 0
    async with db.transaction() as conn:
        approved = await files.approved_runbooks(conn)
    for runbook in approved:
        markdown = (source.data_dir / "runbooks" / runbook["original_filename"]).read_text(encoding="utf-8")
        document = build_runbook_document(
            runbook_id=runbook["runbook_id"],
            service_id=runbook["service_id"],
            title=runbook["title"],
            version=runbook["version"],
            markdown=markdown,
            approved_at=runbook["created_at"],
        )
        queued += await _enqueue_if_changed(db, document, bank_id)
    return {"runbook_documents_queued": queued}
