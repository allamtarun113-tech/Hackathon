from __future__ import annotations

from app.agent.graph import _after_understanding, _after_validation


def test_rejected_input_skips_to_persistence() -> None:
    assert _after_validation({"rejected": True}) == "save_message_and_trace"
    assert _after_validation({"rejected": False}) == "redact_sensitive_data"


def test_missing_context_requests_clarification() -> None:
    assert _after_understanding({"needs_clarification": True}) == "request_clarification"
    assert _after_understanding({"needs_clarification": False}) == "fetch_recent_changes"
