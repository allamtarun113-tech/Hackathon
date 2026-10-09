"""Memory-write guardrails. Nothing reaches Hindsight without passing these checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.hashing import payload_checksum
from app.core.redaction import redact_text

# Anything that looks like held-out evaluation material or hidden labels.
_EVALUATION_MARKERS = re.compile(
    r"\b(EVAL-\d+|DEMO-[A-Z]?\d+|WF-\d{4}|pattern_id|expected_(answer|incident|root_cause|action|extraction)"
    r"|grading rubric|hidden pattern|held[- ]out|novel[-_ ]case label)\b",
    re.IGNORECASE,
)
_MEANINGFUL_OUTCOMES = {"worked", "partial", "did_not_work", "made_worse"}
_MIN_FEEDBACK_NOTE_WORDS = 6


@dataclass(frozen=True)
class MemoryVerdict:
    allowed: bool
    reason: str | None = None


def is_meaningful_feedback(*, outcome: str | None, note: str | None, confirmed: bool) -> MemoryVerdict:
    """Bare thumbs up/down never reaches memory; an outcome must be stated or confirmed by the resolution."""
    if outcome not in _MEANINGFUL_OUTCOMES:
        return MemoryVerdict(False, "no_meaningful_outcome")
    words = len((note or "").split())
    if words < _MIN_FEEDBACK_NOTE_WORDS and not confirmed:
        return MemoryVerdict(False, "bare_rating_without_context")
    return MemoryVerdict(True)


def validate_memory_payload(payload: dict[str, Any], *, expected_checksum: str) -> MemoryVerdict:
    """Final check performed by the worker immediately before submission."""
    if payload_checksum(payload) != expected_checksum:
        return MemoryVerdict(False, "checksum_mismatch")

    item = payload.get("item") or {}
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return MemoryVerdict(False, "empty_content")
    if not payload.get("document_id"):
        return MemoryVerdict(False, "missing_document_id")

    metadata = item.get("metadata") or {}
    if not metadata.get("supabase_record_id") and not metadata.get("runbook_id"):
        return MemoryVerdict(False, "missing_source_record")

    searchable = f"{content}\n{metadata}\n{item.get('tags')}\n{item.get('entities')}"
    if _EVALUATION_MARKERS.search(searchable):
        return MemoryVerdict(False, "evaluation_data_detected")

    if redact_text(content).secret_count:
        return MemoryVerdict(False, "unredacted_secret")

    return MemoryVerdict(True)
