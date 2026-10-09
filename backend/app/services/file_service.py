"""Validate, redact and store uploaded log files."""

from __future__ import annotations

from pathlib import PurePath

from app.core.config import Settings
from app.core.context import new_id
from app.core.errors import GuardrailRejected, NotFoundError, ValidationFailed
from app.core.hashing import sha256_bytes
from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.db.pool import Database
from app.db.repositories import audit, conversations, files, observability, reference
from app.domain.contracts import FileUploadResponse
from app.guardrails.file_guard import validate_upload
from app.memory.signatures import error_signatures
from app.storage.supabase_storage import SupabaseStorage

log = get_logger(__name__)


class FileService:
    """Validated, redacted log uploads with manifests."""

    def __init__(self, db: Database, storage: SupabaseStorage, settings: Settings) -> None:
        self._db = db
        self._storage = storage
        self._settings = settings

    async def store_log(
        self,
        *,
        filename: str,
        data: bytes,
        engineer_id: str,
        conversation_id: str | None,
    ) -> FileUploadResponse:
        settings = self._settings
        check = validate_upload(filename, data, max_bytes=settings.max_upload_bytes)

        async with self._db.transaction() as conn:
            await observability.insert_guardrail_results(conn, None, [check.check.to_record()])
        if not check.accepted or check.text is None:
            raise GuardrailRejected("The file was rejected.", details={"reason": check.check.reason_code})

        redaction = redact_text(check.text)
        body = redaction.text.encode("utf-8")
        file_id = new_id("FILE")
        extension = PurePath(filename).suffix.lower()
        # Ownership is recorded in the manifest (conversation_id), never inferred from the storage path.
        storage_path = f"uploads/{file_id}{extension}"

        async with self._db.transaction() as conn:
            if not await reference.engineer_exists(conn, engineer_id):
                raise ValidationFailed("Unknown engineer.")
            if conversation_id and await conversations.get_conversation(conn, conversation_id) is None:
                raise NotFoundError("Conversation not found.")

        # Only redacted bytes are ever stored.
        await self._storage.upload(settings.evidence_bucket, storage_path, body, content_type=check.mime_type)

        checksum = sha256_bytes(body)
        try:
            async with self._db.transaction() as conn:
                await files.insert_manifest(
                    conn,
                    {
                        "file_id": file_id,
                        "conversation_id": conversation_id,
                        "storage_bucket": settings.evidence_bucket,
                        "storage_path": storage_path,
                        "original_filename": PurePath(filename).name[:255],
                        "mime_type": check.mime_type,
                        "size_bytes": len(body),
                        "sha256": checksum,
                        "source": "upload",
                        "redaction_state": "redacted" if redaction.changed else "clean",
                        "redaction_count": len(redaction.redactions),
                        "uploaded_by": engineer_id,
                    },
                )
                await audit.record(
                    conn,
                    "file.uploaded",
                    resource_type="file",
                    resource_id=file_id,
                    actor_type="engineer",
                    actor_id=engineer_id,
                    details={"size_bytes": len(body), "sha256": checksum},
                )
                if redaction.changed:
                    await audit.record(
                        conn,
                        "redaction.performed",
                        resource_type="file",
                        resource_id=file_id,
                        actor_type="system",
                        details={"count": len(redaction.redactions)},
                    )
        except Exception:
            # Compensate: never leave an object in Storage without a manifest.
            try:
                await self._storage.delete(settings.evidence_bucket, storage_path)
            except Exception as cleanup_error:
                log.error(
                    "orphaned_upload", storage_path=storage_path, error_type=type(cleanup_error).__name__
                )
            raise

        return FileUploadResponse(
            file_id=file_id,
            sha256=checksum,
            size_bytes=len(body),
            mime_type=check.mime_type,
            redaction_count=len(redaction.redactions),
            error_signatures=error_signatures(redaction.text.splitlines()),
        )
