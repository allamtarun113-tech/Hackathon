"""Upload redacted incident logs and versioned runbooks to Supabase Storage with manifests.

Every manifest references an object that was actually uploaded, and its SHA-256
is computed from the exact bytes stored.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.hashing import sha256_bytes
from app.core.redaction import redact_text
from app.db.pool import Database
from app.db.repositories import files
from app.importers.source import SeedSource
from app.storage.supabase_storage import SupabaseStorage


async def import_incident_logs(
    db: Database, storage: SupabaseStorage, source: SeedSource, settings: Settings
) -> dict[str, int]:
    uploaded = 0
    redactions = 0
    for incident in source.incidents:
        relative = incident.get("log_file")
        if not relative:
            continue
        path = source.log_path(relative)
        if not path.exists():
            continue

        redaction = redact_text(path.read_text(encoding="utf-8"))
        body = redaction.text.encode("utf-8")
        object_path = f"incidents/{incident['id']}/{path.name}"
        await storage.upload(settings.evidence_bucket, object_path, body, content_type="text/plain")

        async with db.transaction() as conn:
            await files.insert_manifest(
                conn,
                {
                    "file_id": f"FILE-{incident['id']}-LOG",
                    "incident_id": incident["id"],
                    "storage_bucket": settings.evidence_bucket,
                    "storage_path": object_path,
                    "original_filename": path.name,
                    "mime_type": "text/plain",
                    "size_bytes": len(body),
                    "sha256": sha256_bytes(body),
                    "source": "historical-seed",
                    "redaction_state": "redacted" if redaction.changed else "clean",
                    "redaction_count": len(redaction.redactions),
                },
            )
        uploaded += 1
        redactions += len(redaction.redactions)
    return {"incident_logs": uploaded, "redactions": redactions}


def runbook_title(markdown: str, fallback: str) -> str:
    return next(
        (line.lstrip("# ").strip() for line in markdown.splitlines() if line.startswith("#")), fallback
    )


async def import_runbooks(
    db: Database, storage: SupabaseStorage, source: SeedSource, settings: Settings
) -> dict[str, int]:
    service_ids = set(source.catalog["services"])
    new_versions = 0
    paths = source.runbook_paths()
    for path in paths:
        markdown = path.read_text(encoding="utf-8")
        body = redact_text(markdown, include_pii=False).text.encode("utf-8")
        checksum = sha256_bytes(body)
        runbook_id = f"RB-{path.stem}"
        object_path = f"{path.stem}/{checksum[:12]}.md"
        title = runbook_title(markdown, path.stem)

        await storage.upload(settings.runbook_bucket, object_path, body, content_type="text/markdown")
        async with db.transaction() as conn:
            file_id = f"FILE-{runbook_id}-{checksum[:12]}"
            await files.insert_manifest(
                conn,
                {
                    "file_id": file_id,
                    "storage_bucket": settings.runbook_bucket,
                    "storage_path": object_path,
                    "original_filename": path.name,
                    "mime_type": "text/markdown",
                    "size_bytes": len(body),
                    "sha256": checksum,
                    "source": "runbook",
                    "redaction_state": "clean",
                    "redaction_count": 0,
                },
            )
            created = await files.upsert_runbook_version(
                conn,
                {
                    "runbook_id": runbook_id,
                    "service_id": path.stem if path.stem in service_ids else None,
                    "title": title,
                    "file_id": file_id,
                    "sha256": checksum,
                    "approved_by": "seed-import",
                },
            )
        new_versions += int(created)
    return {"runbooks": len(paths), "new_runbook_versions": new_versions}
