"""Validation for uploaded log files.

Only plain-text logs are accepted. Content is sniffed rather than trusting the
client-supplied MIME type or extension.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePath

from app.guardrails.models import Decision, GuardrailCheck

ALLOWED_EXTENSIONS = {".log", ".txt", ".json", ".jsonl", ".ndjson"}
_BINARY_SIGNATURES = (
    b"\x7fELF",  # ELF executable
    b"MZ",  # Windows PE
    b"PK\x03\x04",  # zip / office documents
    b"%PDF",
    b"\x1f\x8b",  # gzip
    b"\xca\xfe\xba\xbe",  # Mach-O / Java class
    b"#!/",  # scripts
)


@dataclass(frozen=True)
class FileCheck:
    accepted: bool
    mime_type: str
    text: str | None
    check: GuardrailCheck


def _reject(reason: str, **details: object) -> FileCheck:
    return FileCheck(
        accepted=False,
        mime_type="application/octet-stream",
        text=None,
        check=GuardrailCheck("input", "file_validation", Decision.REJECT, reason, dict(details)),
    )


def validate_upload(filename: str, data: bytes, *, max_bytes: int) -> FileCheck:
    extension = PurePath(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return _reject("unsupported_file_type", extension=extension)
    if not data:
        return _reject("empty_file")
    if len(data) > max_bytes:
        return _reject("file_too_large", size_bytes=len(data), max_bytes=max_bytes)
    if data.startswith(_BINARY_SIGNATURES) or b"\x00" in data[:8192]:
        return _reject("binary_content")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _reject("invalid_utf8")

    mime_type = "text/plain"
    if extension == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as error:
            return _reject("corrupted_json", line=error.lineno)
        mime_type = "application/json"
    elif extension in {".jsonl", ".ndjson"}:
        mime_type = "application/x-ndjson"

    return FileCheck(
        accepted=True,
        mime_type=mime_type,
        text=text,
        check=GuardrailCheck("input", "file_validation", Decision.ALLOW, None, {"size_bytes": len(data)}),
    )
