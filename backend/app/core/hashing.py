"""Deterministic checksums and identifiers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

# Fixed namespace so operation IDs are stable across processes and deploys.
_OPERATION_NAMESPACE = uuid.UUID("6f1d3c52-6a0e-4b8e-9d55-6b8f0f6f2a10")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def payload_checksum(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def stable_operation_id(bank_id: str, document_id: str, checksum: str) -> uuid.UUID:
    """The same document content always maps to the same Hindsight operation ID.

    Re-submitting after a crash or timeout is therefore idempotent on the Hindsight side.
    """
    return uuid.uuid5(_OPERATION_NAMESPACE, f"{bank_id}:{document_id}:{checksum}")
