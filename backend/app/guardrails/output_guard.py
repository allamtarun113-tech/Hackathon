"""Output guardrails applied to every recommendation before it reaches the engineer.

Two stages, matching two graph nodes:
  * validate_evidence - citations exist and were retrieved; strong matches are earned.
  * validate_safety   - no execution claims, destructive commands gated, no contradictions.
Then `sanitize.sanitize_tree` redacts and re-checks citations across the complete response.

Invalid content is repaired where that is safe and every intervention is recorded
as a GuardrailCheck.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.recommendation import Recommendation
from app.guardrails.models import Decision, GuardrailCheck

INCIDENT_ID = re.compile(r"\bINC-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")

_EXECUTION_CLAIM = re.compile(
    r"\b(I|we|PagerMind|the agent)\s+(have\s+|has\s+|just\s+|already\s+)?"
    r"(ran|run|executed|restarted|rolled back|applied|deployed|scaled|deleted|flushed|killed|patched|fixed)\b"
    r"|\b(has|have)\s+been\s+(executed|restarted|rolled back|applied|scaled|deleted|flushed)\s+(by me|by PagerMind)\b",
    re.IGNORECASE,
)

_DESTRUCTIVE = re.compile(
    r"(kubectl\s+delete\b|\brm\s+-[a-z]*r[a-z]*f|\bdrop\s+(table|database|schema)\b|\btruncate\s+(table\s+)?\w+"
    r"|\bdelete\s+from\s+\w+(?![\s\S]*\bwhere\b)|--force\b|\bflushall\b|\bflushdb\b|terraform\s+destroy"
    r"|helm\s+(uninstall|delete)\b|git\s+push\s+(-f|--force)|\bkill\s+-9\b|\bdd\s+if=|\bmkfs\b"
    r"|kafka-topics.*--delete|kafka-consumer-groups.*--reset-offsets.*--execute)",
    re.IGNORECASE,
)

_WORDS = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "or",
    "for",
    "in",
    "on",
    "it",
    "is",
    "this",
    "that",
    "with",
}

DESTRUCTIVE_WARNING = (
    "Destructive command: confirm blast radius and get approval from the incident commander before running."
)


@dataclass
class OutputGuardResult:
    recommendation: Recommendation
    checks: list[GuardrailCheck] = field(default_factory=list)
    downgraded: bool = False


# --------------------------------------------------------------------------- text-level checks
def find_incident_ids(text: str) -> set[str]:
    return set(INCIDENT_ID.findall(text))


def claims_execution(text: str) -> bool:
    return bool(_EXECUTION_CLAIM.search(text))


def is_destructive(command_or_text: str) -> bool:
    return bool(_DESTRUCTIVE.search(command_or_text))


def check_output_text(text: str, *, known_incident_ids: set[str]) -> list[GuardrailCheck]:
    """Check free-form model output. Used by the guardrail suite and the baseline answer."""
    checks: list[GuardrailCheck] = []
    unknown = sorted(find_incident_ids(text) - known_incident_ids)
    if unknown:
        checks.append(
            GuardrailCheck(
                "output",
                "citation_validity",
                Decision.REJECT_OUTPUT,
                "unknown_incident_citation",
                {"unknown_ids": unknown},
            )
        )
    if claims_execution(text):
        checks.append(
            GuardrailCheck("output", "execution_claim", Decision.REJECT_OUTPUT, "claims_command_executed")
        )
    if is_destructive(text):
        checks.append(
            GuardrailCheck(
                "output", "destructive_command", Decision.REQUIRE_HUMAN_APPROVAL, "unsafe_mutation"
            )
        )
    return checks


# --------------------------------------------------------------------------- structured validation
def _keywords(text: str) -> set[str]:
    return {word for word in _WORDS.findall(text.lower()) if word not in _STOPWORDS and len(word) > 2}


def _overlaps(a: str, b: str) -> bool:
    left, right = _keywords(a), _keywords(b)
    if not left or not right:
        return False
    return len(left & right) / min(len(left), len(right)) >= 0.6


def strip_execution_claims(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentence for sentence in sentences if not claims_execution(sentence)).strip()


def validate_evidence(
    recommendation: Recommendation,
    *,
    known_incident_ids: set[str],
    retrieved_incident_ids: set[str],
    facts_per_incident: dict[str, int],
    strong_match_min_facts: int,
) -> OutputGuardResult:
    """Evidence stage. `known` = exists in Supabase; `retrieved` = Hindsight returned it in this request."""
    rec = recommendation.model_copy(deep=True)
    result = OutputGuardResult(recommendation=rec)

    # Stable ordering and IDs so feedback can reference a specific step.
    rec.recommended_steps.sort(key=lambda step: step.order)
    for index, step in enumerate(rec.recommended_steps, start=1):
        step.order = index
        step.step_id = step.step_id or f"step-{index}"

    valid_ids = known_incident_ids & retrieved_incident_ids

    # Citations must exist and must have been retrieved in this request.
    invalid = sorted(rec.cited_ids - valid_ids)
    if invalid:
        rec.matched_incidents = [m for m in rec.matched_incidents if m.incident_id in valid_ids]
        for step in rec.recommended_steps:
            step.evidence_ids = [i for i in step.evidence_ids if i in valid_ids]
        for avoid in rec.actions_to_avoid:
            avoid.evidence_ids = [i for i in avoid.evidence_ids if i in valid_ids]
        result.checks.append(
            GuardrailCheck(
                "output",
                "citation_validity",
                Decision.REPAIR_OUTPUT,
                "unknown_incident_citation",
                {"removed_ids": invalid},
            )
        )
        result.downgraded = True

    # Invented IDs mentioned in prose are neutralised as well.
    for attribute in ("summary", "similarity_explanation", "likely_cause"):
        text = getattr(rec, attribute)
        for incident_id in find_incident_ids(text) - valid_ids:
            text = text.replace(incident_id, "[unverified incident]")
        setattr(rec, attribute, text)

    # A strong match needs adequate supporting evidence.
    supporting = sum(facts_per_incident.get(m.incident_id, 0) for m in rec.matched_incidents)
    if rec.is_strong_match and (not rec.matched_incidents or supporting < strong_match_min_facts):
        rec.is_strong_match = False
        rec.confidence = min(rec.confidence, 0.4)
        result.downgraded = True
        result.checks.append(
            GuardrailCheck(
                "output",
                "match_strength",
                Decision.REPAIR_OUTPUT,
                "weak_match_reported_as_strong",
                {"supporting_facts": supporting},
            )
        )

    # Novel or weak incidents must abstain explicitly.
    if not rec.is_strong_match:
        rec.confidence = min(rec.confidence, 0.5)
        if not rec.abstention_reason:
            rec.abstention_reason = (
                "No past incident matches this error signature closely enough. This problem appears new; "
                "the steps below are diagnostic hypotheses."
            )

    # Steps without evidence are hypotheses, never confirmed fixes.
    for step in rec.recommended_steps:
        if not step.evidence_ids:
            step.is_hypothesis = True

    if not result.checks:
        result.checks.append(GuardrailCheck("output", "evidence_guard", Decision.ALLOW))
    return result


def validate_safety(recommendation: Recommendation, *, escalation_team: str | None) -> OutputGuardResult:
    """Safety stage: execution claims, destructive commands and contradictions.

    Secret redaction and citation clean-up of every field happen afterwards in
    `app.guardrails.sanitize.sanitize_tree`, which walks the whole response object.
    """
    rec = recommendation.model_copy(deep=True)
    result = OutputGuardResult(recommendation=rec)

    # No claims that PagerMind executed anything.
    claim_found = False
    for step in rec.recommended_steps:
        for attribute in ("action", "rationale"):
            value = getattr(step, attribute)
            if claims_execution(value):
                setattr(step, attribute, strip_execution_claims(value) or "Suggested action (not executed).")
                claim_found = True
    if claims_execution(rec.summary):
        rec.summary = strip_execution_claims(rec.summary) or "PagerMind suggests the steps below."
        claim_found = True
    if claim_found:
        result.checks.append(
            GuardrailCheck("output", "execution_claim", Decision.REPAIR_OUTPUT, "claims_command_executed")
        )

    # Destructive commands are blocked behind human approval and escalated.
    flagged: list[str] = []
    for step in rec.recommended_steps:
        if is_destructive(f"{step.action} {step.command or ''}"):
            step.requires_approval = True
            step.warning = DESTRUCTIVE_WARNING
            flagged.append(step.step_id)
    if flagged:
        rec.escalation_team = rec.escalation_team or escalation_team
        result.checks.append(
            GuardrailCheck(
                "output",
                "destructive_command",
                Decision.REQUIRE_HUMAN_APPROVAL,
                "unsafe_mutation",
                {"step_ids": flagged},
            )
        )

    # Never recommend and avoid the same action.
    conflicting = [
        avoid.action
        for avoid in rec.actions_to_avoid
        if any(_overlaps(avoid.action, step.action) for step in rec.recommended_steps)
    ]
    if conflicting:
        rec.actions_to_avoid = [a for a in rec.actions_to_avoid if a.action not in conflicting]
        result.checks.append(
            GuardrailCheck(
                "output",
                "contradiction",
                Decision.REPAIR_OUTPUT,
                "conflicting_recommendations",
                {"removed": conflicting},
            )
        )

    if not result.checks:
        result.checks.append(GuardrailCheck("output", "safety_guard", Decision.ALLOW))
    return result
