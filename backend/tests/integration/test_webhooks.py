"""Webhook verification, replay protection and concurrent at-least-once deliveries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx

from app.db.pool import Database
from app.db.repositories import memory
from tests.integration.conftest import WEBHOOK_SECRET, Externals
from tests.integration.test_outbox_and_worker import _document, _enqueue


def _signed(body: bytes, *, at: int | None = None) -> dict[str, str]:
    timestamp = at if at is not None else int(time.time())
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Hindsight-Signature-V2": f"t={timestamp},v1={signature}", "Content-Type": "application/json"}


async def _submitted_job(db: Database, operation_id: str) -> str:
    job = await _enqueue(db, _document())
    async with db.transaction() as conn:
        await memory.claim_ready_jobs(conn, worker_id="w", limit=1)
        await memory.mark_submitted(conn, job["job_id"], operation_id)
    return str(job["job_id"])


def _payload(operation_id: str, units: int = 6) -> bytes:
    return json.dumps(
        {
            "event": "retain.completed",
            "bank_id": "pagermind-test",
            "operation_id": operation_id,
            "status": "completed",
            "data": {"document_id": "incident:INC-2025", "memory_unit_count": units},
        }
    ).encode()


async def test_valid_delivery_completes_the_job(
    app_client: httpx.AsyncClient, db: Database, externals: Externals
) -> None:
    job_id = await _submitted_job(db, "op-1")
    externals.hindsight.complete("op-1", units=6)
    body = _payload("op-1")
    response = await app_client.post("/webhooks/hindsight", content=body, headers=_signed(body))
    assert response.json() == {"status": "processed"}
    async with db.transaction() as conn:
        job = await memory.get_job(conn, job_id)
    assert job is not None and job["status"] == "retained" and job["memory_unit_count"] == 6


async def test_replays_and_legacy_signatures_are_rejected(
    app_client: httpx.AsyncClient, db: Database, externals: Externals
) -> None:
    await _submitted_job(db, "op-2")
    externals.hindsight.complete("op-2", units=3)
    body = _payload("op-2")

    stale = await app_client.post(
        "/webhooks/hindsight", content=body, headers=_signed(body, at=int(time.time()) - 3600)
    )
    legacy_sig = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    legacy = await app_client.post(
        "/webhooks/hindsight", content=body, headers={"X-Hindsight-Signature": f"sha256={legacy_sig}"}
    )
    assert stale.status_code == 401
    assert legacy.status_code == 401


async def test_concurrent_duplicate_deliveries_are_processed_once(
    app_client: httpx.AsyncClient, db: Database, externals: Externals
) -> None:
    job_id = await _submitted_job(db, "op-3")
    externals.hindsight.complete("op-3", units=4)
    body = _payload("op-3")

    responses = await asyncio.gather(
        *(app_client.post("/webhooks/hindsight", content=body, headers=_signed(body)) for _ in range(6))
    )
    statuses = sorted(response.json()["status"] for response in responses)
    assert statuses.count("processed") == 1
    assert statuses.count("duplicate") == 5

    async with db.transaction() as conn:
        cursor = await conn.execute(
            "select count(*) as n from audit_events where event_type = 'memory_job.completed' and resource_id = %s",
            (job_id,),
        )
        row = await cursor.fetchone()
    assert row is not None and row["n"] == 1


async def test_zero_unit_result_is_not_marked_retained(
    app_client: httpx.AsyncClient, db: Database, externals: Externals
) -> None:
    job_id = await _submitted_job(db, "op-4")
    externals.hindsight.complete("op-4", units=0)
    body = _payload("op-4", units=0)
    await app_client.post("/webhooks/hindsight", content=body, headers=_signed(body))
    async with db.transaction() as conn:
        job = await memory.get_job(conn, job_id)
    assert job is not None and job["status"] == "needs_review" and job["memory_unit_count"] == 0


async def test_unknown_operations_are_ignored(app_client: httpx.AsyncClient) -> None:
    body = _payload("op-unknown")
    response = await app_client.post("/webhooks/hindsight", content=body, headers=_signed(body))
    assert response.json() == {"status": "ignored"}


async def test_memory_defense_block_rejects_the_job_instead_of_retrying(
    app_client: httpx.AsyncClient, db: Database
) -> None:
    job_id = await _submitted_job(db, "op-defense")
    body = json.dumps(
        {
            # Documented shape: https://hindsight.vectorize.io/developer/api/webhooks
            "event": "memory_defense.triggered",
            "bank_id": "pagermind-test",
            "operation_id": "op-defense",
            "status": "block",
            "timestamp": "2026-10-10T00:00:00Z",
            "data": {
                "action": "block",
                "detector": "sensitive_data",
                "document_id": "incident:INC-2025",
                "matched_types": ["github_token"],
                "message": "Blocked: content contains ghp_AAAA...BBBB",
            },
        }
    ).encode()
    response = await app_client.post("/webhooks/hindsight", content=body, headers=_signed(body))
    assert response.json() == {"status": "memory_defense_recorded"}
    async with db.transaction() as conn:
        job = await memory.get_job(conn, job_id)
        cursor = await conn.execute(
            "select details from audit_events where event_type = 'memory_defense.triggered' and resource_id = %s",
            (job_id,),
        )
        audit_row = await cursor.fetchone()
    assert (
        job is not None
        and job["status"] == "rejected"
        and job["rejection_reason"] == "memory_defense_blocked"
    )
    details = audit_row["details"] if audit_row else {}
    assert details["detector"] == "sensitive_data" and details["matched_types"] == ["github_token"]
    assert "message" not in details  # text that may quote the content never reaches the audit log


async def test_enterprise_violation_event_is_matched_by_document(
    app_client: httpx.AsyncClient, db: Database
) -> None:
    job_id = await _submitted_job(db, "op-siem")
    body = json.dumps(
        {
            "event": "memory_defense.violation",
            "bank_id": "pagermind-test",
            "data": {
                "detector": "GitHub Token",
                "severity": "high",
                "action_taken": "BLOCK",
                "document_id": "incident:INC-2025",
                "hits": [{"detector": "GitHub Token", "preview": "ghp_AAAA...BBBB"}],
            },
        }
    ).encode()
    response = await app_client.post("/webhooks/hindsight", content=body, headers=_signed(body))
    assert response.json() == {"status": "memory_defense_recorded"}
    async with db.transaction() as conn:
        job = await memory.get_job(conn, job_id)
    assert job is not None and job["status"] == "rejected"


async def test_redaction_is_recorded_but_the_job_continues(
    app_client: httpx.AsyncClient, db: Database
) -> None:
    job_id = await _submitted_job(db, "op-redact")
    body = json.dumps(
        {
            "event": "memory_defense.triggered",
            "operation_id": "op-redact",
            "status": "redact",
            "data": {"action": "redact", "detector": "sensitive_data", "document_id": "incident:INC-2025"},
        }
    ).encode()
    await app_client.post("/webhooks/hindsight", content=body, headers=_signed(body))
    async with db.transaction() as conn:
        job = await memory.get_job(conn, job_id)
    assert job is not None and job["status"] == "submitted"
