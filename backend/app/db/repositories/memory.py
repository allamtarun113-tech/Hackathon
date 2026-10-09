"""Memory outbox, memory documents, Hindsight operations and webhook events.

The outbox is the only path into Hindsight: request handlers enqueue, the
memory worker submits, and the webhook (or the reconciler) completes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from app.db.json import jsonb
from app.db.pool import Connection

TERMINAL_STATES = ("retained", "needs_review", "rejected", "dead_letter")
# Jobs that ended without searchable memory. They need a human, not a retry.
ATTENTION_STATES = ("needs_review", "dead_letter", "rejected")


async def enqueue(conn: Connection, job: dict[str, Any]) -> dict[str, Any]:
    """Insert a job; identical content for the same document is deduplicated by checksum."""
    cursor = await conn.execute(
        """
        insert into memory_outbox (job_id, bank_id, document_id, source_type, source_id, update_mode,
                                   payload, payload_checksum, operation_id)
        values (%(job_id)s, %(bank_id)s, %(document_id)s, %(source_type)s, %(source_id)s, %(update_mode)s,
                %(payload)s, %(payload_checksum)s, %(operation_id)s)
        on conflict (bank_id, document_id, payload_checksum) do update set updated_at = memory_outbox.updated_at
        returning job_id, status, (xmax = 0) as created
        """,
        {**job, "payload": jsonb(job["payload"])},
    )
    row = await cursor.fetchone()
    assert row is not None
    await conn.execute(
        """
        insert into memory_documents (document_id, bank_id, memory_type, source_type, source_id, last_job_id)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (document_id) do update set last_job_id = excluded.last_job_id
        """,
        (
            job["document_id"],
            job["bank_id"],
            job["memory_type"],
            job["source_type"],
            job["source_id"],
            row["job_id"],
        ),
    )
    return row


async def claim_ready_jobs(conn: Connection, *, worker_id: str, limit: int) -> list[dict[str, Any]]:
    """Atomically claim due jobs. SKIP LOCKED lets several workers run safely.

    Jobs are serialised per document: a job is only claimable when every earlier job for the
    same document has reached a terminal state, so an older version can never overwrite a newer one.
    """
    cursor = await conn.execute(
        """
        with ready as (
            select o.job_id from memory_outbox o
            where o.status in ('pending', 'failed')
              and o.next_attempt_at <= now()
              and o.attempt_count < o.max_attempts
              and not exists (
                  select 1 from memory_outbox earlier
                  where earlier.document_id = o.document_id
                    and earlier.bank_id = o.bank_id
                    and earlier.enqueue_seq < o.enqueue_seq
                    and earlier.status not in ('retained', 'needs_review', 'rejected', 'dead_letter')
              )
            order by o.enqueue_seq
            limit %s
            for update of o skip locked
        )
        update memory_outbox o
        set status = 'validating', locked_by = %s, locked_at = now(), attempt_count = o.attempt_count + 1
        from ready
        where o.job_id = ready.job_id
        returning o.*
        """,
        (limit, worker_id),
    )
    return await cursor.fetchall()


async def release_stale_locks(conn: Connection, *, older_than: timedelta) -> int:
    """Return jobs abandoned by a crashed worker to the queue."""
    cursor = await conn.execute(
        """
        update memory_outbox
        set status = 'failed', locked_by = null, locked_at = null, last_error = 'worker lock expired'
        where status = 'validating' and locked_at < now() - %s
        """,
        (older_than,),
    )
    return cursor.rowcount


async def mark_rejected(conn: Connection, job_id: str, reason: str) -> None:
    await conn.execute(
        """
        update memory_outbox
        set status = 'rejected', rejection_reason = %s, completed_at = now(), locked_by = null
        where job_id = %s
        """,
        (reason, job_id),
    )
    await _sync_document(conn, job_id, "failed")


async def mark_submitted(conn: Connection, job_id: str, hindsight_operation_id: str | None) -> None:
    await conn.execute(
        """
        update memory_outbox
        set status = 'submitted', hindsight_operation_id = %s, submitted_at = now(), locked_by = null,
            last_error = null
        where job_id = %s
        """,
        (hindsight_operation_id, job_id),
    )


async def mark_processing(conn: Connection, job_id: str) -> None:
    await conn.execute(
        "update memory_outbox set status = 'processing' where job_id = %s and status = 'submitted'", (job_id,)
    )


async def complete_job(conn: Connection, job_id: str, memory_unit_count: int | None) -> str | None:
    """Record a successful Hindsight operation.

    A zero extracted-unit result is not searchable memory, so the job goes to `needs_review`
    instead of `retained`. Returns the new status, or None if the job was already terminal
    (a duplicate completion).
    """
    status = "needs_review" if memory_unit_count == 0 else "retained"
    cursor = await conn.execute(
        """
        update memory_outbox
        set status = %s, memory_unit_count = %s, completed_at = now(), last_error = null,
            rejection_reason = case when %s = 'needs_review' then 'zero memory units extracted' end
        where job_id = %s and status not in ('retained', 'needs_review', 'rejected', 'dead_letter')
        """,
        (status, memory_unit_count, status, job_id),
    )
    if cursor.rowcount == 0:
        return None
    await _sync_document(conn, job_id, status)
    return status


async def mark_failed(conn: Connection, job_id: str, error: str, *, retry_in: timedelta) -> str:
    """Schedule a retry, or move the job to the dead-letter state once attempts are exhausted."""
    cursor = await conn.execute(
        """
        update memory_outbox
        set status = case when attempt_count >= max_attempts then 'dead_letter' else 'failed' end,
            last_error = left(%s, 2000),
            next_attempt_at = now() + %s,
            locked_by = null,
            completed_at = case when attempt_count >= max_attempts then now() end
        where job_id = %s
        returning status
        """,
        (error, retry_in, job_id),
    )
    row = await cursor.fetchone()
    status: str = row["status"] if row else "failed"
    if status == "dead_letter":
        await _sync_document(conn, job_id, "failed")
    return status


async def _sync_document(conn: Connection, job_id: str, status: str) -> None:
    await conn.execute(
        """
        update memory_documents d
        set status = %s,
            current_checksum = case when %s = 'retained' then o.payload_checksum else d.current_checksum end,
            retained_at = case when %s = 'retained' then now() else d.retained_at end
        from memory_outbox o
        where o.job_id = %s and d.document_id = o.document_id and d.last_job_id = o.job_id
        """,
        (status, status, status, job_id),
    )


async def get_job(conn: Connection, job_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute(
        """
        select job_id, bank_id, document_id, source_type, source_id, status, attempt_count, max_attempts,
               hindsight_operation_id, operation_id, last_error, rejection_reason, memory_unit_count,
               created_at, submitted_at, completed_at, updated_at
        from memory_outbox where job_id = %s
        """,
        (job_id,),
    )
    return await cursor.fetchone()


async def jobs_needing_attention(
    conn: Connection, *, statuses: tuple[str, ...] = ATTENTION_STATES, limit: int = 50
) -> list[dict[str, Any]]:
    """Silent empty retains look like Hindsight 'knows' something it does not; surface them to operators."""
    cursor = await conn.execute(
        """
        select job_id, bank_id, document_id, source_type, source_id, status, attempt_count, max_attempts,
               hindsight_operation_id, operation_id, last_error, rejection_reason, memory_unit_count,
               created_at, submitted_at, completed_at, updated_at
        from memory_outbox
        where status = any(%s)
        order by updated_at desc
        limit %s
        """,
        (list(statuses), limit),
    )
    return await cursor.fetchall()


async def inflight_job_for_document(conn: Connection, document_id: str) -> dict[str, Any] | None:
    """The newest not-yet-finished job for a document (jobs per document are processed in order)."""
    cursor = await conn.execute(
        """
        select * from memory_outbox
        where document_id = %s and status not in ('retained', 'needs_review', 'rejected', 'dead_letter')
        order by created_at desc
        limit 1
        """,
        (document_id,),
    )
    return await cursor.fetchone()


async def job_by_operation(conn: Connection, hindsight_operation_id: str) -> dict[str, Any] | None:
    """Find a job by the ID Hindsight returned, falling back to our own stable operation UUID.

    Two indexed lookups instead of one `OR` with a cast, which would force a sequential scan.
    """
    cursor = await conn.execute(
        "select * from memory_outbox where hindsight_operation_id = %s", (hindsight_operation_id,)
    )
    job = await cursor.fetchone()
    if job is not None:
        return job
    try:
        operation_uuid = uuid.UUID(hindsight_operation_id)
    except ValueError:
        return None
    cursor = await conn.execute("select * from memory_outbox where operation_id = %s", (operation_uuid,))
    return await cursor.fetchone()


async def inflight_jobs(conn: Connection, *, submitted_before: datetime, limit: int) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select * from memory_outbox
        where status in ('submitted', 'processing') and submitted_at < %s
        order by submitted_at
        limit %s
        for update skip locked
        """,
        (submitted_before, limit),
    )
    return await cursor.fetchall()


async def record_operation_status(
    conn: Connection,
    *,
    hindsight_operation_id: str,
    job_id: str,
    status: str,
    memory_unit_count: int | None,
    error_message: str | None,
) -> None:
    await conn.execute(
        """
        insert into memory_operations (hindsight_operation_id, job_id, status, memory_unit_count, error_message)
        values (%s, %s, %s, %s, %s)
        on conflict (hindsight_operation_id) do update set
            status = excluded.status,
            memory_unit_count = excluded.memory_unit_count,
            error_message = excluded.error_message,
            checked_at = now()
        """,
        (hindsight_operation_id, job_id, status, memory_unit_count, error_message),
    )


async def record_webhook_event(
    conn: Connection,
    *,
    hindsight_operation_id: str | None,
    event_type: str,
    bank_id: str | None,
    signature_valid: bool,
    payload: dict[str, Any],
) -> str:
    cursor = await conn.execute(
        """
        insert into memory_webhook_events (hindsight_operation_id, event_type, bank_id, signature_valid, payload)
        values (%s, %s, %s, %s, %s)
        returning webhook_event_id
        """,
        (hindsight_operation_id, event_type, bank_id, signature_valid, jsonb(payload)),
    )
    row = await cursor.fetchone()
    assert row is not None
    return str(row["webhook_event_id"])


async def claim_webhook_event(
    conn: Connection, *, hindsight_operation_id: str, event_type: str, webhook_event_id: str
) -> bool:
    """Atomically claim processing of an (operation, event) pair. Exactly one delivery wins.

    Called inside the transaction that applies the result, so a failure releases the claim
    and Hindsight's redelivery can try again.
    """
    cursor = await conn.execute(
        """
        insert into memory_webhook_claims (hindsight_operation_id, event_type, webhook_event_id)
        values (%s, %s, %s)
        on conflict do nothing
        """,
        (hindsight_operation_id, event_type, webhook_event_id),
    )
    return cursor.rowcount == 1


async def finish_webhook_event(
    conn: Connection, webhook_event_id: str, status: str, error: str | None = None
) -> None:
    await conn.execute(
        """
        update memory_webhook_events
        set processing_status = %s, error = %s, processed_at = now()
        where webhook_event_id = %s
        """,
        (status, error, webhook_event_id),
    )


async def outbox_counts(conn: Connection) -> dict[str, int]:
    cursor = await conn.execute("select status, job_count from memory_outbox_summary")
    return {row["status"]: int(row["job_count"]) for row in await cursor.fetchall()}


async def document_checksum(conn: Connection, document_id: str) -> str | None:
    cursor = await conn.execute(
        "select current_checksum from memory_documents where document_id = %s and status = 'retained'",
        (document_id,),
    )
    row = await cursor.fetchone()
    return row["current_checksum"] if row else None


async def mark_source_retained(conn: Connection, job: dict[str, Any]) -> None:
    """Reflect a completed retain back onto the source record."""
    if job["source_type"] == "feedback":
        await conn.execute(
            "update feedback set retain_status = 'retained' where feedback_id = %s", (job["source_id"],)
        )


# --------------------------------------------------------------------------- worker heartbeats
async def heartbeat(
    conn: Connection,
    *,
    worker_id: str,
    component: str,
    version: str,
    started_at: datetime,
    jobs_processed: int,
) -> None:
    await conn.execute(
        """
        insert into worker_heartbeats (worker_id, component, version, started_at, last_seen_at, jobs_processed)
        values (%s, %s, %s, %s, now(), %s)
        on conflict (worker_id) do update
        set last_seen_at = now(), jobs_processed = excluded.jobs_processed, version = excluded.version
        """,
        (worker_id, component, version, started_at, jobs_processed),
    )


async def latest_heartbeat(conn: Connection, component: str) -> dict[str, Any] | None:
    cursor = await conn.execute(
        """
        select worker_id, last_seen_at, extract(epoch from now() - last_seen_at)::int as age_seconds
        from worker_heartbeats where component = %s
        order by last_seen_at desc limit 1
        """,
        (component,),
    )
    return await cursor.fetchone()


async def documents_for_source(conn: Connection, source_id: str) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select document_id, memory_type, status, retained_at from memory_documents
        where source_id = %s order by updated_at desc
        """,
        (source_id,),
    )
    return await cursor.fetchall()


_SOURCE_LOOKUPS = {
    "incident": "select 1 from incidents where incident_id = %s",
    "resolution": "select 1 from incidents where incident_id = %s",
    "feedback": "select 1 from feedback where feedback_id = %s",
    "runbook": "select 1 from runbook_versions where runbook_id = %s",
}


async def source_exists(conn: Connection, *, source_type: str, source_id: str) -> bool:
    """A memory document must always have its exact record in Supabase."""
    cursor = await conn.execute(_SOURCE_LOOKUPS[source_type], (source_id,))
    return await cursor.fetchone() is not None


async def lease_for_reconcile(conn: Connection, job_ids: list[str]) -> None:
    """Push the in-flight timestamp forward so other workers skip these jobs until the next interval."""
    if job_ids:
        await conn.execute("update memory_outbox set submitted_at = now() where job_id = any(%s)", (job_ids,))
