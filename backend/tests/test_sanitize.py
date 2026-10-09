from __future__ import annotations

from app.domain.recommendation import BaselineAnswer, RecommendedStep
from app.guardrails.sanitize import TreeReport, redact_tree, sanitize_baseline, sanitize_tree


def test_redaction_reaches_every_nested_string() -> None:
    payload = {
        "recommendation": {
            "summary": "ok",
            "recommended_steps": [{"rationale": "token in log password=hunter2-secret", "command": None}],
            "abstention_reason": "call ops@example.com",
        },
        "evidence": [{"text": "Bearer abcdefghijklmnopqrstuvwxyz0123"}],
        "count": 3,
    }
    report = TreeReport()
    cleaned = redact_tree(payload, report)
    flat = str(cleaned)
    assert "hunter2-secret" not in flat
    assert "ops@example.com" not in flat
    assert "abcdefghijklmnopqrstuvwxyz0123" not in flat
    assert cleaned["count"] == 3
    assert report.redactions == 3


def test_unknown_citations_are_neutralised_anywhere() -> None:
    report = TreeReport()
    cleaned = sanitize_tree(
        {"escalation_team": "see INC-9999", "matched": [{"why": "like INC-2025"}]},
        allowed_incident_ids={"INC-2025"},
        report=report,
    )
    assert "INC-9999" not in str(cleaned)
    assert "INC-2025" in str(cleaned)
    assert report.removed_incident_ids == {"INC-9999"}


def test_baseline_cannot_cite_claim_or_run_unflagged_destructive_commands() -> None:
    baseline = BaselineAnswer(
        summary="I restarted the pods. This looks like INC-2025.",
        recommended_steps=[
            RecommendedStep(
                order=1, action="Drop the namespace", command="kubectl delete namespace payments"
            ),
        ],
    )
    safe, checks = sanitize_baseline(baseline)
    assert "I restarted" not in safe.summary
    assert "INC-2025" not in safe.summary
    step = safe.recommended_steps[0]
    assert step.requires_approval and step.warning and step.is_hypothesis
    assert {check.reason_code for check in checks} >= {"claims_command_executed", "unknown_incident_citation"}
