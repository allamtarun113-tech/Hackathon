"""Recursive sanitisation of complete response objects.

Field-by-field checks miss whatever a schema change adds next. These helpers walk
every string in a nested structure: summary, causes, actions, commands, rationales,
evidence facts, citations, baseline answers, abstention text and escalation notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.redaction import redact_text
from app.domain.recommendation import BaselineAnswer
from app.guardrails.models import Decision, GuardrailCheck
from app.guardrails.output_guard import (
    DESTRUCTIVE_WARNING,
    claims_execution,
    find_incident_ids,
    is_destructive,
    strip_execution_claims,
)

UNVERIFIED_INCIDENT = "[unverified incident]"


@dataclass
class TreeReport:
    redactions: int = 0
    removed_incident_ids: set[str] = field(default_factory=set)


def _walk(value: Any, transform: Any) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_walk(item, transform) for item in value]
    if isinstance(value, tuple):
        return tuple(_walk(item, transform) for item in value)
    if isinstance(value, dict):
        return {key: _walk(item, transform) for key, item in value.items()}
    return value


def redact_tree(value: Any, report: TreeReport | None = None) -> Any:
    """Redact secrets and personal data from every string in `value`."""

    def transform(text: str) -> str:
        result = redact_text(text)
        if report is not None:
            report.redactions += len(result.redactions)
        return result.text

    return _walk(value, transform)


def sanitize_tree(value: Any, *, allowed_incident_ids: set[str], report: TreeReport | None = None) -> Any:
    """Redact every string and neutralise incident IDs that are not in `allowed_incident_ids`."""
    report = report if report is not None else TreeReport()

    def transform(text: str) -> str:
        redacted = redact_text(text)
        report.redactions += len(redacted.redactions)
        cleaned = redacted.text
        for incident_id in find_incident_ids(cleaned) - allowed_incident_ids:
            report.removed_incident_ids.add(incident_id)
            cleaned = cleaned.replace(incident_id, UNVERIFIED_INCIDENT)
        return cleaned

    return _walk(value, transform)


def tree_checks(report: TreeReport, *, scope: str) -> list[GuardrailCheck]:
    checks: list[GuardrailCheck] = []
    if report.redactions:
        checks.append(
            GuardrailCheck(
                "output",
                f"{scope}_redaction",
                Decision.REDACT_AND_CONTINUE,
                "secret_detected",
                {"count": report.redactions},
            )
        )
    if report.removed_incident_ids:
        checks.append(
            GuardrailCheck(
                "output",
                f"{scope}_citations",
                Decision.REPAIR_OUTPUT,
                "unknown_incident_citation",
                {"removed_ids": sorted(report.removed_incident_ids)},
            )
        )
    return checks


def sanitize_baseline(baseline: BaselineAnswer) -> tuple[BaselineAnswer, list[GuardrailCheck]]:
    """The baseline has no memory, so it may not cite any incident, claim execution or run unflagged
    destructive commands."""
    answer = baseline.model_copy(deep=True)
    checks: list[GuardrailCheck] = []

    claimed = False
    if claims_execution(answer.summary):
        answer.summary = strip_execution_claims(answer.summary) or "Suggested steps below."
        claimed = True
    for step in answer.recommended_steps:
        for attribute in ("action", "rationale"):
            value = getattr(step, attribute)
            if claims_execution(value):
                setattr(step, attribute, strip_execution_claims(value) or "Suggested action (not executed).")
                claimed = True
        step.evidence_ids = []
        step.is_hypothesis = True
        if is_destructive(f"{step.action} {step.command or ''}"):
            step.requires_approval = True
            step.warning = DESTRUCTIVE_WARNING
    if claimed:
        checks.append(
            GuardrailCheck(
                "output", "baseline_execution_claim", Decision.REPAIR_OUTPUT, "claims_command_executed"
            )
        )

    report = TreeReport()
    cleaned = sanitize_tree(answer.model_dump(), allowed_incident_ids=set(), report=report)
    checks += tree_checks(report, scope="baseline")
    return BaselineAnswer.model_validate(cleaned), checks


def sanitize_recommendation(
    recommendation: dict[str, Any], *, allowed_incident_ids: set[str], report: TreeReport | None = None
) -> dict[str, Any]:
    """Final pass over a recommendation.

    Structured citation fields (matched incidents, evidence IDs) are *filtered*, never rewritten,
    so the result always still validates; free text is then redacted and scrubbed of unverified IDs.
    """
    report = report if report is not None else TreeReport()
    rec = dict(recommendation)

    matched = []
    for match in rec.get("matched_incidents", []):
        if match.get("incident_id") in allowed_incident_ids:
            matched.append(match)
        else:
            report.removed_incident_ids.add(str(match.get("incident_id")))
    rec["matched_incidents"] = matched

    for key in ("recommended_steps", "actions_to_avoid"):
        items = []
        for item in rec.get(key, []):
            kept = [i for i in item.get("evidence_ids", []) if i in allowed_incident_ids]
            report.removed_incident_ids.update(set(item.get("evidence_ids", [])) - set(kept))
            items.append({**item, "evidence_ids": kept})
        rec[key] = items

    cleaned: dict[str, Any] = sanitize_tree(rec, allowed_incident_ids=allowed_incident_ids, report=report)
    return cleaned
