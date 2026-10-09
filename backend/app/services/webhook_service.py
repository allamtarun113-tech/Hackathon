"""Process Hindsight webhook deliveries.

Steps: verify the timestamped signature -> record the delivery -> confirm the result
with the operations API -> in one transaction, atomically claim the (operation, event)
pair and apply it -> audit. Deliveries are at least once; the claim makes concurrent and
repeated deliveries safe, and a failure before commit releases the claim for redelivery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.pool import Database
from app.db.repositories import audit, memory
from app.memory.client import HindsightGateway
from app.memory.webhooks import verify_signature
from app.observability.sentry import capture
from app.services.memory_completion import apply_operation_status

log = get_logger(__name__)

HANDLED_EVENT = "retain.completed"
# Memory Defense, per https://hindsight.vectorize.io/developer/api/webhooks:
#   memory_defense.triggered  data: action (redact|block), detector, document_id, matched_types, message
# Cloud Enterprise SIEM deliveries use memory_defense.violation with `action_taken` instead of `action`.
DEFENSE_EVENTS = frozenset({"memory_defense.triggered", "memory_defense.violation"})
# Only these fields are copied into the audit log. `message`, `hits` and previews may quote the content.
_DEFENSE_FIELDS = ("detector", "severity", "document_id")


def defense_details(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("data")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    details: dict[str, Any] = {key: str(data[key])[:100] for key in _DEFENSE_FIELDS if data.get(key)}
    action = data.get("action") or data.get("action_taken") or payload.get("status") or ""
    details["action"] = str(action).lower()[:20]
    matched = data.get("matched_types")
    if isinstance(matched, list):
        details["matched_types"] = [str(kind)[:50] for kind in matched[:10]]
    return details


class MemoryDefenseTriggered(Exception):
    """Hindsight Memory Defense redacted or blocked content on its way into the bank."""


@dataclass(frozen=True)
class WebhookOutcome:
    status: str
    http_status: int = 200


class InvalidWebhookSignatureError(Exception):
    pass


def _parse(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class WebhookService:
    """Signed Hindsight webhook deliveries."""

    def __init__(self, db: Database, hindsight: HindsightGateway, settings: Settings) -> None:
        self._db = db
        self._hindsight = hindsight
        self._settings = settings

    async def handle(self, *, body: bytes, headers: dict[str, str]) -> WebhookOutcome:
        valid = verify_signature(
            body=body,
            headers=headers,
            secret=self._settings.hindsight_webhook_secret.get_secret_value(),
            tolerance_seconds=self._settings.hindsight_webhook_tolerance_seconds,
        )
        payload = _parse(body) if valid else {}
        event_type = str(payload.get("event", "unknown"))
        operation_id = payload.get("operation_id")

        event_id, job = await self._record_delivery(valid=valid, payload=payload, event_type=event_type)
        if not valid:
            capture(InvalidWebhookSignatureError("Invalid Hindsight webhook signature"), component="webhook")
            log.warning("webhook_signature_invalid")
            return WebhookOutcome("invalid_signature", 401)
        if event_type in DEFENSE_EVENTS:
            return await self._handle_defense(event_id, payload, job)
        if job is None or not operation_id:
            return WebhookOutcome("ignored")

        # Never trust the webhook body alone: confirm with the operations API (outside any transaction).
        try:
            status = await self._hindsight.operation_status(operation_id)
        except Exception as error:  # Hindsight redelivers; the worker's reconciler is the backstop.
            async with self._db.transaction() as conn:
                await memory.finish_webhook_event(conn, event_id, "error", type(error).__name__)
            return WebhookOutcome("verification_deferred", 503)

        unit_count = status.memory_unit_count
        if unit_count is None:
            reported = (payload.get("data") or {}).get("memory_unit_count")
            unit_count = reported if isinstance(reported, int) else None

        async with self._db.transaction() as conn:
            if not await memory.claim_webhook_event(
                conn, hindsight_operation_id=operation_id, event_type=event_type, webhook_event_id=event_id
            ):
                await memory.finish_webhook_event(conn, event_id, "duplicate")
                return WebhookOutcome("duplicate")

            await memory.record_operation_status(
                conn,
                hindsight_operation_id=operation_id,
                job_id=job["job_id"],
                status=status.status,
                memory_unit_count=unit_count,
                error_message=status.error_message,
            )
            await apply_operation_status(
                conn,
                job,
                status=status.status,
                error_message=status.error_message,
                memory_unit_count=unit_count,
                actor="webhook",
                retry_in=timedelta(minutes=5),
            )
            await memory.finish_webhook_event(conn, event_id, "processed")
        return WebhookOutcome("processed")

    async def _record_delivery(
        self, *, valid: bool, payload: dict[str, Any], event_type: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Store every delivery for audit and resolve the outbox job it refers to (if any)."""
        operation_id = payload.get("operation_id")
        async with self._db.transaction() as conn:
            event_id = await memory.record_webhook_event(
                conn,
                hindsight_operation_id=operation_id,
                event_type=event_type,
                bank_id=payload.get("bank_id"),
                signature_valid=valid,
                payload=payload if valid else {"redacted": "invalid or replayed signature"},
            )
            await audit.record(
                conn,
                "webhook.received",
                resource_type="memory_webhook_event",
                resource_id=event_id,
                actor_type="webhook",
                details={"event": event_type, "signature_valid": valid},
            )
            if not valid:
                await memory.finish_webhook_event(conn, event_id, "error", "invalid or replayed signature")
                return event_id, None
            if event_type in DEFENSE_EVENTS:
                job = await memory.job_by_operation(conn, operation_id) if operation_id else None
                document_id = (payload.get("data") or {}).get("document_id")
                if job is None and isinstance(document_id, str):
                    job = await memory.inflight_job_for_document(conn, document_id)
                return event_id, job
            if event_type != HANDLED_EVENT or not operation_id:
                await memory.finish_webhook_event(conn, event_id, "ignored")
                return event_id, None
            job = await memory.job_by_operation(conn, operation_id)
            if job is None:
                await memory.finish_webhook_event(conn, event_id, "ignored", "unknown operation")
            return event_id, job

    async def _handle_defense(
        self, event_id: str, payload: dict[str, Any], job: dict[str, Any] | None
    ) -> WebhookOutcome:
        """Make Memory Defense visible: audit it, alert on it, and stop retrying content it blocked.

        A blocked retain would otherwise fail, back off and retry until it dead-letters, while looking
        like a transient Hindsight outage. Rejecting it puts it straight into the operator review queue.
        """
        details = defense_details(payload)
        blocked = details["action"] == "block"
        # Never undo a job Hindsight already finished; a block only stops one still in flight.
        rejectable = job is not None and blocked and job["status"] not in memory.TERMINAL_STATES
        async with self._db.transaction() as conn:
            if job is not None and rejectable:
                await memory.mark_rejected(conn, job["job_id"], "memory_defense_blocked")
            await audit.record(
                conn,
                "memory_defense.triggered",
                resource_type="memory_outbox" if job else "memory_webhook_event",
                resource_id=job["job_id"] if job else event_id,
                actor_type="webhook",
                details={**details, "operation_id": payload.get("operation_id"), "job_rejected": rejectable},
            )
            await memory.finish_webhook_event(conn, event_id, "processed")
        log.warning("memory_defense_triggered", job_id=job["job_id"] if job else None, **details)
        capture(MemoryDefenseTriggered(details.get("detector", "unknown detector")), component="webhook")
        return WebhookOutcome("memory_defense_recorded")
