"""File manifests, attachments and runbook versions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.db.pool import Connection


async def insert_manifest(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into file_manifests (
            file_id, incident_id, conversation_id, storage_bucket, storage_path, original_filename, mime_type,
            size_bytes, sha256, source, redaction_state, redaction_count, uploaded_by, schema_version
        ) values (
            %(file_id)s, %(incident_id)s, %(conversation_id)s, %(storage_bucket)s, %(storage_path)s,
            %(original_filename)s, %(mime_type)s, %(size_bytes)s, %(sha256)s, %(source)s, %(redaction_state)s,
            %(redaction_count)s, %(uploaded_by)s, %(schema_version)s
        )
        on conflict (storage_bucket, storage_path) do update set
            size_bytes = excluded.size_bytes,
            sha256 = excluded.sha256,
            redaction_state = excluded.redaction_state,
            redaction_count = excluded.redaction_count
        """,
        {"conversation_id": None, "incident_id": None, "uploaded_by": None, "schema_version": "1.0", **row},
    )
    if row.get("incident_id"):
        await conn.execute(
            "insert into incident_attachments (incident_id, file_id) values (%s, %s) on conflict do nothing",
            (row["incident_id"], row["file_id"]),
        )


async def get_manifest(conn: Connection, file_id: str) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "select * from file_manifests where file_id = %s and deleted_at is null", (file_id,)
    )
    return await cursor.fetchone()


async def upsert_runbook_version(conn: Connection, row: dict[str, Any]) -> bool:
    """Returns True when this content is a new version (checksum changed)."""
    cursor = await conn.execute(
        "select sha256 from runbook_versions where runbook_id = %s order by version desc limit 1",
        (row["runbook_id"],),
    )
    latest = await cursor.fetchone()
    if latest and latest["sha256"] == row["sha256"]:
        return False
    await conn.execute(
        "update runbook_versions set status = 'superseded' where runbook_id = %s and status = 'approved'",
        (row["runbook_id"],),
    )
    await conn.execute(
        """
        insert into runbook_versions (runbook_id, version, service_id, title, file_id, sha256, status, approved_by)
        select %(runbook_id)s, coalesce(max(version), 0) + 1, %(service_id)s, %(title)s, %(file_id)s, %(sha256)s,
               'approved', %(approved_by)s
        from runbook_versions where runbook_id = %(runbook_id)s
        """,
        row,
    )
    return True


async def claim_uploads(
    conn: Connection,
    file_ids: list[str],
    *,
    conversation_id: str,
    engineer_id: str,
    ttl: timedelta,
) -> set[str]:
    """Attach uploads to a conversation. Returns the file IDs that now belong to it.

    A file qualifies if it already belongs to this conversation, or if it is an unclaimed
    upload by the same engineer within the claim window. Anything else (another
    conversation's file, a seed file, an expired upload) is not claimed.
    """
    if not file_ids:
        return set()
    cursor = await conn.execute(
        """
        update file_manifests
        set conversation_id = %(conversation_id)s
        where file_id = any(%(file_ids)s)
          and source = 'upload'
          and deleted_at is null
          and redaction_state <> 'rejected'
          and (
                conversation_id = %(conversation_id)s
             or (conversation_id is null and uploaded_by = %(engineer_id)s and uploaded_at > now() - %(ttl)s)
          )
        returning file_id
        """,
        {"file_ids": file_ids, "conversation_id": conversation_id, "engineer_id": engineer_id, "ttl": ttl},
    )
    return {row["file_id"] for row in await cursor.fetchall()}


async def approved_runbooks(conn: Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        select r.runbook_id, r.version, r.service_id, r.title, r.created_at, m.original_filename
        from runbook_versions r
        join file_manifests m using (file_id)
        where r.status = 'approved'
        """
    )
    return await cursor.fetchall()
