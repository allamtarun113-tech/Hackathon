"""Memory worker: drains the outbox into Hindsight, reconciles in-flight jobs,
records a heartbeat and writes periodic health snapshots.

Lifecycle per job
    pending -> validating -> submitted -> processing -> retained
                                                      \\-> needs_review   (zero memory units extracted)
                         \\-> rejected                 \\-> failed -> retry with backoff -> dead_letter

Jobs for the same document are processed strictly in enqueue order. The webhook
normally completes jobs; the reconciler polls the operations API for jobs that
stayed in flight too long, so a lost webhook never strands a job.

Run with:  python -m app.workers.memory_worker
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app import __version__
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.pool import Database
from app.db.repositories import audit, memory, observability, rate_limits
from app.db.repositories.circuit_breakers import PostgresBreakerStore
from app.guardrails.memory_guard import MemoryVerdict, validate_memory_payload
from app.memory.client import HindsightGateway, is_permanent_rejection
from app.observability.sentry import capture, init_sentry
from app.services.health_service import MEMORY_WORKER_COMPONENT, overall_status, probe_all
from app.services.memory_completion import apply_operation_status
from app.storage.supabase_storage import SupabaseStorage

log = get_logger("memory_worker")

COMPONENT = MEMORY_WORKER_COMPONENT

GatewayFactory = Callable[[str], HindsightGateway]


def backoff(settings: Settings, attempt: int) -> timedelta:
    """Exponential backoff capped at one hour."""
    return timedelta(seconds=min(3600, settings.worker_base_backoff_seconds * 2 ** max(0, attempt - 1)))


def _hindsight_item(payload: dict[str, Any]) -> dict[str, Any]:
    item = dict(payload["item"])
    if isinstance(item.get("timestamp"), str):
        item["timestamp"] = datetime.fromisoformat(item["timestamp"])
    return item


class MemoryWorker:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        gateway_factory: GatewayFactory,
        storage: SupabaseStorage | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.storage = storage
        self._gateway_factory = gateway_factory
        self._gateways: dict[str, HindsightGateway] = {}
        self.worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.started_at = datetime.now(UTC)
        self.jobs_processed = 0
        self._last_maintenance = 0.0
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        for gateway in self._gateways.values():
            await gateway.close()

    def gateway(self, bank_id: str) -> HindsightGateway:
        if bank_id not in self._gateways:
            self._gateways[bank_id] = self._gateway_factory(bank_id)
        return self._gateways[bank_id]

    async def run(self) -> None:
        log.info("worker_started", worker_id=self.worker_id)
        while not self._stopping.is_set():
            processed = 0
            try:
                processed = await self.process_batch()
                await self.reconcile_inflight()
                await self.beat()
                await self.maybe_run_maintenance()
            except Exception as error:
                log.exception("worker_iteration_failed", error_type=type(error).__name__)
                capture(error, component=COMPONENT)
            if processed == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.worker_poll_interval_seconds
                    )
        log.info("worker_stopped", worker_id=self.worker_id)

    # ------------------------------------------------------------------ liveness
    async def beat(self) -> None:
        async with self.db.transaction() as conn:
            await memory.heartbeat(
                conn,
                worker_id=self.worker_id,
                component=COMPONENT,
                version=__version__,
                started_at=self.started_at,
                jobs_processed=self.jobs_processed,
            )

    async def maybe_run_maintenance(self) -> None:
        """Periodic work: a health snapshot (never produced by public requests) and retention pruning."""
        if time.monotonic() - self._last_maintenance < self.settings.health_snapshot_interval_seconds:
            return
        self._last_maintenance = time.monotonic()
        await self._snapshot_health()
        async with self.db.transaction() as conn:
            await rate_limits.prune(conn, older_than=timedelta(hours=1))
            removed = await observability.prune_expired(
                conn,
                webhook_days=self.settings.retention_webhook_days,
                health_snapshot_days=self.settings.retention_health_snapshot_days,
                guardrail_result_days=self.settings.retention_guardrail_result_days,
            )
        if any(removed.values()):
            log.info("retention_pruned", **removed)

    async def _snapshot_health(self) -> None:
        if self.storage is None:
            return
        dependencies = await probe_all(
            self.settings, self.db, self.storage, self.gateway(self.settings.hindsight_bank_id)
        )
        async with self.db.transaction() as conn:
            counts = await memory.outbox_counts(conn)
            await observability.insert_health_snapshot(
                conn,
                overall_status=overall_status(dependencies),
                dependencies={name: dep.model_dump() for name, dep in dependencies.items()},
                pending_memory_jobs=counts.get("pending", 0) + counts.get("failed", 0),
            )

    # ------------------------------------------------------------------ submission
    async def process_batch(self) -> int:
        async with self.db.transaction() as conn:
            await memory.release_stale_locks(conn, older_than=timedelta(minutes=5))
            jobs = await memory.claim_ready_jobs(
                conn, worker_id=self.worker_id, limit=self.settings.worker_batch_size
            )
        for job in jobs:
            await self._process(job)
        self.jobs_processed += len(jobs)
        return len(jobs)

    async def _process(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        verdict = validate_memory_payload(job["payload"], expected_checksum=job["payload_checksum"])
        if verdict.allowed and not await self._source_exists(job):
            verdict = MemoryVerdict(False, "source_record_missing")
        if not verdict.allowed:
            async with self.db.transaction() as conn:
                await memory.mark_rejected(conn, job_id, verdict.reason or "rejected")
                await audit.record(
                    conn,
                    "memory_job.rejected",
                    resource_type="memory_outbox",
                    resource_id=job_id,
                    actor_type="worker",
                    details={"reason": verdict.reason},
                )
            log.warning("memory_job_rejected", job_id=job_id, reason=verdict.reason)
            return

        try:
            hindsight_operation_id = await self.gateway(job["bank_id"]).submit_retain(
                document_id=job["document_id"],
                item=_hindsight_item(job["payload"]),
                operation_id=str(job["operation_id"]),
            )
        except Exception as error:
            if is_permanent_rejection(error):
                # e.g. Memory Defense blocked every item (422): retrying would only reach dead_letter.
                reason = f"hindsight_rejected_{getattr(error, 'status', 'unknown')}"
                async with self.db.transaction() as conn:
                    await memory.mark_rejected(conn, job_id, reason)
                    await audit.record(
                        conn,
                        "memory_job.rejected",
                        resource_type="memory_outbox",
                        resource_id=job_id,
                        actor_type="worker",
                        details={"reason": reason},
                    )
                log.warning("memory_job_rejected_by_hindsight", job_id=job_id, reason=reason)
                return
            async with self.db.transaction() as conn:
                status = await memory.mark_failed(
                    conn,
                    job_id,
                    f"{type(error).__name__}: {error}"[:500],
                    retry_in=backoff(self.settings, job["attempt_count"]),
                )
                await audit.record(
                    conn,
                    "memory_job.submit_failed",
                    resource_type="memory_outbox",
                    resource_id=job_id,
                    actor_type="worker",
                    details={"status": status, "attempt": job["attempt_count"]},
                )
            log.warning(
                "memory_job_submit_failed", job_id=job_id, status=status, error_type=type(error).__name__
            )
            if status == "dead_letter":
                capture(error, component=COMPONENT, job_id=job_id)
            return

        async with self.db.transaction() as conn:
            await memory.mark_submitted(conn, job_id, hindsight_operation_id)
            await audit.record(
                conn,
                "memory_job.submitted",
                resource_type="memory_outbox",
                resource_id=job_id,
                actor_type="worker",
                details={"operation_id": hindsight_operation_id},
            )
        log.info("memory_job_submitted", job_id=job_id, document_id=job["document_id"])

    async def _source_exists(self, job: dict[str, Any]) -> bool:
        async with self.db.transaction() as conn:
            return await memory.source_exists(
                conn, source_type=job["source_type"], source_id=job["source_id"]
            )

    # ------------------------------------------------------------------ reconciliation
    async def reconcile_inflight(self) -> None:
        """Lease stale in-flight jobs, then check them without holding row locks during network calls."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.worker_reconcile_after_seconds)
        async with self.db.transaction() as conn:
            jobs = await memory.inflight_jobs(
                conn, submitted_before=cutoff, limit=self.settings.worker_batch_size
            )
            await memory.lease_for_reconcile(conn, [job["job_id"] for job in jobs])
        for job in jobs:
            await self._reconcile(job)

    async def _reconcile(self, job: dict[str, Any]) -> None:
        operation_id = job["hindsight_operation_id"] or str(job["operation_id"])
        try:
            status = await self.gateway(job["bank_id"]).operation_status(operation_id)
        except Exception as error:
            log.warning("reconcile_status_failed", job_id=job["job_id"], error_type=type(error).__name__)
            return

        async with self.db.transaction() as conn:
            await memory.record_operation_status(
                conn,
                hindsight_operation_id=operation_id,
                job_id=job["job_id"],
                status=status.status,
                memory_unit_count=status.memory_unit_count,
                error_message=status.error_message,
            )
            await apply_operation_status(
                conn,
                job,
                status=status.status,
                error_message=status.error_message,
                memory_unit_count=status.memory_unit_count,
                actor="worker",
                retry_in=backoff(self.settings, job["attempt_count"]),
            )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env != "development")
    init_sentry(settings, component=COMPONENT)

    db = Database(settings, role="worker")
    await db.open()
    storage = SupabaseStorage(settings)
    worker = MemoryWorker(
        settings,
        db,
        gateway_factory=lambda bank_id: HindsightGateway(
            settings, bank_id=bank_id, breaker_store=PostgresBreakerStore(db)
        ),
        storage=storage,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await worker.close()
        await storage.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
