"""Outbox ordering, deduplication, retries, multi-worker claims and the worker lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.db.pool import Database
from app.db.repositories import memory
from app.memory.documents import (
    IncidentRecord,
    MemoryDocument,
    build_incident_document,
)
from app.services.outbox import enqueue_document
from app.workers.memory_worker import MemoryWorker
from tests.fakes import FakeHindsight
from tests.integration.conftest import Externals, build_client, operator_headers


def _document(incident_id: str = "INC-2025", resolution: str = "Suspended recon-job.") -> MemoryDocument:
    return build_incident_document(
        IncidentRecord(
            incident_id=incident_id,
            title="HikariCP pool exhausted",
            service_id="payments-api",
            team="Payments",
            severity="SEV1",
            environment="production",
            started_at=datetime(2026, 1, 10, 2, 40, tzinfo=UTC),
            alert_title="p99 latency",
            alert_value="10s",
            customer_impact="Checkout failures",
            error_signatures=["HikariPool-1 - Connection is not available"],
            actions=[],
            root_cause="recon-job held connections",
            resolution=resolution,
            resolved_by="Priya Sharma",
            mttr_minutes=20,
            follow_ups=[],
        )
    )


async def _enqueue(db: Database, document: MemoryDocument) -> dict[str, Any]:
    async with db.transaction() as conn:
        return await enqueue_document(conn, document, bank_id="pagermind-test")


async def _claim(db: Database, worker: str, limit: int = 10) -> list[dict[str, Any]]:
    async with db.transaction() as conn:
        return await memory.claim_ready_jobs(conn, worker_id=worker, limit=limit)


async def test_identical_content_is_deduplicated(db: Database) -> None:
    first = await _enqueue(db, _document())
    second = await _enqueue(db, _document())
    assert first["created"] and not second["created"]
    assert first["job_id"] == second["job_id"]


async def test_jobs_for_one_document_are_processed_in_order(db: Database) -> None:
    older = await _enqueue(db, _document(resolution="Version one of the postmortem."))
    newer = await _enqueue(db, _document(resolution="Version two of the postmortem."))

    claimed = await _claim(db, "w1")
    assert [job["job_id"] for job in claimed] == [older["job_id"]]
    assert await _claim(db, "w2") == []  # newer waits while older is in flight

    async with db.transaction() as conn:
        assert await memory.complete_job(conn, older["job_id"], 7) == "retained"
    assert [job["job_id"] for job in await _claim(db, "w2")] == [newer["job_id"]]


async def test_concurrent_workers_never_claim_the_same_job(db: Database) -> None:
    for index in range(12):
        await _enqueue(db, _document(incident_id=f"INC-X{index}"))

    results = await asyncio.gather(*(_claim(db, f"worker-{n}", limit=4) for n in range(4)))
    claimed = [job["job_id"] for batch in results for job in batch]
    assert len(claimed) == len(set(claimed))
    assert len(claimed) == 12


async def test_zero_extracted_units_needs_review_and_duplicates_are_ignored(db: Database) -> None:
    job = await _enqueue(db, _document())
    await _claim(db, "w1")
    async with db.transaction() as conn:
        assert await memory.complete_job(conn, job["job_id"], 0) == "needs_review"
        assert await memory.complete_job(conn, job["job_id"], 5) is None
        stored = await memory.get_job(conn, job["job_id"])
        cursor = await conn.execute(
            "select status from memory_documents where document_id = 'incident:INC-2025'"
        )
        document = await cursor.fetchone()
    assert stored is not None and stored["status"] == "needs_review" and stored["memory_unit_count"] == 0
    assert document is not None and document["status"] == "needs_review"


async def test_operators_see_retains_without_searchable_memory(
    settings: Settings, db: Database, externals: Externals
) -> None:
    empty, good = await _enqueue(db, _document("INC-2025")), await _enqueue(db, _document("INC-2030"))
    async with db.transaction() as conn:
        assert await memory.complete_job(conn, empty["job_id"], 0) == "needs_review"
        assert await memory.complete_job(conn, good["job_id"], 7) == "retained"

    async with build_client(settings, externals) as client:
        response = await client.get("/memory-jobs", headers=operator_headers())
        filtered = await client.get("/memory-jobs?status=dead_letter", headers=operator_headers())

    assert response.status_code == 200
    jobs = response.json()
    assert [job["job_id"] for job in jobs] == [empty["job_id"]]
    assert jobs[0]["rejection_reason"] == "zero memory units extracted"
    assert filtered.json() == []


async def test_failures_back_off_then_dead_letter(db: Database) -> None:
    job = await _enqueue(db, _document())
    async with db.transaction() as conn:
        await conn.execute("update memory_outbox set max_attempts = 2 where job_id = %s", (job["job_id"],))
    statuses = []
    for _ in range(2):
        async with db.transaction() as conn:
            await conn.execute(
                "update memory_outbox set next_attempt_at = now() where job_id = %s", (job["job_id"],)
            )
        await _claim(db, "w1")
        async with db.transaction() as conn:
            statuses.append(
                await memory.mark_failed(conn, job["job_id"], "boom", retry_in=timedelta(seconds=1))
            )
    assert statuses == ["failed", "dead_letter"]


async def test_worker_submits_rejects_and_reconciles(settings: Settings, db: Database) -> None:
    gateway = FakeHindsight()
    worker = MemoryWorker(
        settings.model_copy(update={"worker_reconcile_after_seconds": 0}),
        db,
        gateway_factory=lambda _bank: gateway,
    )  # type: ignore[arg-type,return-value]

    good = await _enqueue(db, _document())
    poisoned = _document(incident_id="INC-X1")
    poisoned.item["content"] += "\nexpected_incident_ids: EVAL-001"
    bad = await _enqueue(db, poisoned)

    assert await worker.process_batch() == 2
    async with db.transaction() as conn:
        good_job = await memory.get_job(conn, good["job_id"])
        bad_job = await memory.get_job(conn, bad["job_id"])
    assert good_job is not None and good_job["status"] == "submitted"
    assert bad_job is not None and bad_job["status"] == "rejected"
    assert len(gateway.submitted) == 1

    gateway.complete(str(good_job["hindsight_operation_id"]), units=0)
    await worker.reconcile_inflight()
    await worker.beat()
    async with db.transaction() as conn:
        reconciled = await memory.get_job(conn, good["job_id"])
        heartbeat = await memory.latest_heartbeat(conn, "memory_worker")
    assert reconciled is not None and reconciled["status"] == "needs_review"
    assert heartbeat is not None and heartbeat["age_seconds"] < 5


async def test_submit_errors_are_retried_with_backoff(settings: Settings, db: Database) -> None:
    gateway = FakeHindsight(submit_error=ConnectionError("hindsight down"))
    worker = MemoryWorker(settings, db, gateway_factory=lambda _bank: gateway)  # type: ignore[arg-type,return-value]
    job = await _enqueue(db, _document())
    await worker.process_batch()
    async with db.transaction() as conn:
        stored = await memory.get_job(conn, job["job_id"])
        cursor = await conn.execute(
            "select next_attempt_at > now() as later from memory_outbox where job_id = %s", (job["job_id"],)
        )
        timing = await cursor.fetchone()
    assert stored is not None and stored["status"] == "failed" and stored["attempt_count"] == 1
    assert timing is not None and timing["later"]


async def test_permanent_hindsight_rejection_is_not_retried(settings: Settings, db: Database) -> None:
    from hindsight_client_api.exceptions import ApiException

    blocked = ApiException(status=422, reason="all items blocked by memory defense")
    worker = MemoryWorker(settings, db, gateway_factory=lambda _bank: FakeHindsight(submit_error=blocked))  # type: ignore[arg-type,return-value]
    job = await _enqueue(db, _document())
    await worker.process_batch()
    async with db.transaction() as conn:
        stored = await memory.get_job(conn, job["job_id"])
    assert stored is not None and stored["status"] == "rejected"
    assert stored["rejection_reason"] == "hindsight_rejected_422"
