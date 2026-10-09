"""Enqueue memory documents into the transactional outbox.

Callers enqueue inside the same transaction that saves the source record, so a
memory write can never exist without its exact Supabase record (and vice versa).
"""

from __future__ import annotations

from typing import Any

from app.core.context import new_id
from app.core.hashing import payload_checksum, stable_operation_id
from app.db.pool import Connection
from app.db.repositories import audit, memory
from app.memory.documents import MemoryDocument


async def enqueue_document(
    conn: Connection,
    document: MemoryDocument,
    *,
    bank_id: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    payload = document.payload()
    checksum = payload_checksum(payload)
    job = {
        "job_id": new_id("MEMJOB"),
        "bank_id": bank_id,
        "document_id": document.document_id,
        "memory_type": document.memory_type,
        "source_type": document.source_type,
        "source_id": document.source_id,
        "update_mode": document.update_mode,
        "payload": payload,
        "payload_checksum": checksum,
        "operation_id": stable_operation_id(bank_id, document.document_id, checksum),
    }
    row = await memory.enqueue(conn, job)
    if row["created"]:
        await audit.record(
            conn,
            "memory_job.created",
            resource_type="memory_outbox",
            resource_id=row["job_id"],
            actor_type="engineer" if actor_id else "system",
            actor_id=actor_id,
            details={"document_id": document.document_id, "source_type": document.source_type},
        )
    return {"job_id": row["job_id"], "status": row["status"], "created": row["created"]}
