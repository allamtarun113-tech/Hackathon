from __future__ import annotations

from app.domain.recommendation import ActionToAvoid, MatchedIncident, Recommendation, RecommendedStep
from app.guardrails.output_guard import check_output_text, validate_evidence, validate_safety


def _recommendation(**overrides: object) -> Recommendation:
    base: dict[str, object] = {
        "summary": "Connection pool exhausted by the reconciliation job, as in INC-2025.",
        "is_strong_match": True,
        "matched_incidents": [MatchedIncident(incident_id="INC-2025", why_similar="Same HikariCP timeout")],
        "likely_cause": "recon-job holds connections",
        "confidence": 0.9,
        "recommended_steps": [
            RecommendedStep(
                order=1,
                action="Suspend the recon-job cronjob",
                rationale="Freed the pool before",
                command='kubectl -n payments patch cronjob recon-job -p \'{"spec":{"suspend":true}}\'',
                evidence_ids=["INC-2025"],
            ),
        ],
        "actions_to_avoid": [
            ActionToAvoid(action="Restart payments pods", reason="Did not help", evidence_ids=["INC-2025"])
        ],
    }
    base.update(overrides)
    return Recommendation.model_validate(base)


def test_invented_citations_are_removed_and_match_downgraded() -> None:
    rec = _recommendation(matched_incidents=[MatchedIncident(incident_id="INC-9999", why_similar="made up")])
    result = validate_evidence(
        rec,
        known_incident_ids={"INC-2025"},
        retrieved_incident_ids={"INC-2025"},
        facts_per_incident={"INC-2025": 3},
        strong_match_min_facts=2,
    )
    assert not result.recommendation.matched_incidents
    assert not result.recommendation.is_strong_match
    assert result.recommendation.abstention_reason
    assert any(c.reason_code == "unknown_incident_citation" for c in result.checks)


def test_strong_match_requires_retrieved_evidence() -> None:
    result = validate_evidence(
        _recommendation(),
        known_incident_ids={"INC-2025"},
        retrieved_incident_ids=set(),
        facts_per_incident={},
        strong_match_min_facts=2,
    )
    assert not result.recommendation.is_strong_match
    assert all(step.is_hypothesis for step in result.recommendation.recommended_steps)


def test_valid_strong_match_is_preserved() -> None:
    result = validate_evidence(
        _recommendation(),
        known_incident_ids={"INC-2025"},
        retrieved_incident_ids={"INC-2025"},
        facts_per_incident={"INC-2025": 4},
        strong_match_min_facts=2,
    )
    assert result.recommendation.is_strong_match
    assert result.recommendation.recommended_steps[0].step_id == "step-1"


def test_destructive_commands_require_approval_and_escalate() -> None:
    rec = _recommendation(
        recommended_steps=[
            RecommendedStep(
                order=1, action="Delete the namespace", command="kubectl delete namespace payments --force"
            ),
        ]
    )
    result = validate_safety(rec, escalation_team="Payments")
    step = result.recommendation.recommended_steps[0]
    assert step.requires_approval and step.warning
    assert result.recommendation.escalation_team == "Payments"


def test_contradictory_avoid_entries_are_removed() -> None:
    rec = _recommendation(
        recommended_steps=[RecommendedStep(order=1, action="Restart payments pods")],
        actions_to_avoid=[ActionToAvoid(action="Restart payments pods", reason="contradiction")],
    )
    result = validate_safety(rec, escalation_team=None)
    assert not result.recommendation.actions_to_avoid


def test_execution_claims_are_stripped() -> None:
    rec = _recommendation(summary="I restarted the pods. The pool should recover.")
    result = validate_safety(rec, escalation_team=None)
    assert "I restarted" not in result.recommendation.summary


def test_text_checks_cover_guardrail_categories() -> None:
    known = {"INC-2025"}
    assert check_output_text("Seen before in INC-FAKE-001", known_incident_ids=known)[0].reason_code == (
        "unknown_incident_citation"
    )
    assert any(
        c.reason_code == "claims_command_executed"
        for c in check_output_text("I ran kubectl rollout restart", known_incident_ids=known)
    )
    assert any(
        c.reason_code == "unsafe_mutation"
        for c in check_output_text("Run: kubectl delete namespace payments", known_incident_ids=known)
    )


def test_over_long_model_output_is_trimmed_not_discarded() -> None:
    rec = Recommendation.model_validate(
        {
            "summary": "x" * 5000,
            "is_strong_match": False,
            "confidence": 1.7,
            "recommended_steps": [{"order": index + 1, "action": f"step {index}"} for index in range(12)],
        }
    )
    assert len(rec.summary) == 1200
    assert rec.confidence == 1.0
    assert len(rec.recommended_steps) == 8
