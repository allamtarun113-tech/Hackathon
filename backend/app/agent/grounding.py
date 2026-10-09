"""The memory-grounded answer path, shared by the live graph and the evaluation runner.

Live and evaluation must call Hindsight identically (same query, context, tag scope and
budget) and must apply the same evidence and safety guardrails, otherwise an evaluation
measures a different system from the one engineers use.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.agent import prompts
from app.core.logging import get_logger
from app.domain.recommendation import MatchStrength, Recommendation, reflect_response_schema
from app.guardrails.models import GuardrailCheck
from app.guardrails.output_guard import validate_evidence
from app.memory.bank import REFLECT_SCOPE_TAGS
from app.memory.client import Budget, HindsightGateway, MemoryFact, ReflectResult

log = get_logger(__name__)

LOG_EXCERPT_LINES = 80


def reflect_budget(severity: str | None) -> Budget:
    """SEV1 gets Hindsight's deepest search; everything else stays at `mid` to hold the p95 latency target."""
    return "high" if (severity or "").upper() == "SEV1" else "mid"


async def grounded_reflect(
    hindsight: HindsightGateway,
    *,
    service_id: str | None,
    severity: str | None,
    symptoms: str,
    error_signatures: list[str],
    recent_changes: list[dict[str, Any]],
    log_text: str,
) -> ReflectResult:
    log_excerpt = "\n".join(log_text.splitlines()[-LOG_EXCERPT_LINES:])
    return await hindsight.reflect(
        query=prompts.reflect_query(
            service_id=service_id, symptoms=symptoms, error_signatures=error_signatures
        ),
        context=prompts.reflect_context(recent_changes=recent_changes, log_excerpt=log_excerpt),
        response_schema=reflect_response_schema(),
        tags=REFLECT_SCOPE_TAGS,
        budget=reflect_budget(severity),
    )


def fact_to_dict(fact: MemoryFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "text": fact.text,
        "incident_id": fact.incident_id,
        "document_id": fact.document_id,
        "occurred_at": fact.occurred_at,
        "tags": fact.tags,
        "score": fact.score,
        "fact_type": fact.fact_type,
        "memory_type": fact.memory_type,
    }


def evidence_dicts(result: ReflectResult) -> list[dict[str, Any]]:
    """Everything Hindsight based the answer on: extracted facts and observations, then mental models."""
    return [fact_to_dict(fact) for fact in [*result.facts, *result.mental_models]]


# Consolidated knowledge (observations, mental models) has no source document, only text that names incidents.
CONSOLIDATED_TYPES = frozenset({"observation", "mental_model"})
_INCIDENT_ID = re.compile(r"\bINC-[A-Z0-9]{2,}\b")


def incident_support(facts: list[dict[str, Any]]) -> tuple[Counter[str], set[str]]:
    """(support per incident for match strength, every incident Hindsight surfaced).

    Support counts postmortem facts plus each observation or mental model that names the incident.
    Verified step feedback marks an incident as retrieved but adds no support: the same outcome is
    already part of the incident's postmortem, and counting it twice would inflate a strong match.
    """
    support: Counter[str] = Counter()
    surfaced: set[str] = set()
    for fact in facts:
        if fact.get("fact_type") in CONSOLIDATED_TYPES:
            named = set(_INCIDENT_ID.findall(str(fact.get("text", ""))))
            support.update(named)
            surfaced |= named
        elif fact.get("incident_id"):
            surfaced.add(fact["incident_id"])
            if fact.get("memory_type") != "feedback":
                support[fact["incident_id"]] += 1
    return support, surfaced


def parse_reflect(
    structured: dict[str, Any] | None, text: str | None
) -> tuple[Recommendation, dict[str, Any] | None]:
    """Validate Hindsight's structured answer; an invalid one becomes an explicit low-confidence abstention."""
    if structured:
        try:
            return Recommendation.model_validate(structured), None
        except ValidationError as error:
            log.warning("reflect_schema_invalid", errors=error.error_count())
    recommendation = Recommendation(
        summary=(text or "Hindsight returned no structured answer.")[:1000],
        is_strong_match=False,
        confidence=0.2,
        abstention_reason="The memory answer could not be validated against the response schema.",
    )
    check = {
        "phase": "output",
        "check_name": "schema_validation",
        "decision": "repair_output",
        "reason_code": "invalid_structured_output",
        "details": {},
    }
    return recommendation, check


@dataclass
class GroundedAnswer:
    recommendation: Recommendation
    match_strength: MatchStrength
    retrieved_incident_ids: list[str]
    facts_per_incident: Counter[str]
    checks: list[GuardrailCheck] = field(default_factory=list)


def ground(
    recommendation: Recommendation,
    *,
    facts: list[dict[str, Any]],
    known_incident_ids: set[str],
    strong_match_min_facts: int,
) -> GroundedAnswer:
    """Citation and match-strength guardrails: only incidents Hindsight actually retrieved may be cited."""
    facts_per_incident, retrieved = incident_support(facts)
    result = validate_evidence(
        recommendation,
        known_incident_ids=known_incident_ids,
        retrieved_incident_ids=retrieved,
        facts_per_incident=dict(facts_per_incident),
        strong_match_min_facts=strong_match_min_facts,
    )
    rec = result.recommendation
    strength: MatchStrength = (
        "strong" if rec.is_strong_match else ("weak" if rec.matched_incidents else "none")
    )
    return GroundedAnswer(
        recommendation=rec,
        match_strength=strength,
        retrieved_incident_ids=sorted(retrieved & known_incident_ids),
        facts_per_incident=facts_per_incident,
        checks=list(result.checks),
    )


# --------------------------------------------------------------------------- explainability
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-]{2,}")
SIGNATURE_OVERLAP = 0.6


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def signatures_in_memory(
    signatures: list[str], facts: list[dict[str, Any]], incident_ids: set[str]
) -> list[str]:
    """Signatures whose distinctive tokens mostly recur in a fact from one of `incident_ids`."""
    fact_tokens = [
        _tokens(str(fact.get("text", ""))) for fact in facts if fact.get("incident_id") in incident_ids
    ]
    found = []
    for signature in signatures:
        wanted = _tokens(signature)
        if wanted and any(len(wanted & tokens) >= SIGNATURE_OVERLAP * len(wanted) for tokens in fact_tokens):
            found.append(signature)
    return found


def memory_summary(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """'This answer used N memories from M incidents' plus the breakdown by memory and fact type."""
    by_memory_type = Counter(str(fact.get("memory_type") or "unknown") for fact in facts)
    return {
        "memories": len(facts),
        "incidents": len(incident_support(facts)[1]),
        "observations": sum(1 for fact in facts if fact.get("fact_type") == "observation"),
        "mental_models": sum(1 for fact in facts if fact.get("fact_type") == "mental_model"),
        "by_memory_type": dict(by_memory_type),
    }


def match_explanation(
    strength: MatchStrength,
    *,
    recommendation: Recommendation,
    facts: list[dict[str, Any]],
    error_signatures: list[str],
    facts_per_incident: Counter[str],
    strong_match_min_facts: int,
) -> str:
    """One sentence, tied to signatures, on why this was a strong, weak or novel match."""
    matched = [m.incident_id for m in recommendation.matched_incidents]
    if strength == "unavailable":
        return "Hindsight was unreachable, so nothing could be matched against past incidents."
    if strength == "strong":
        shared = signatures_in_memory(error_signatures, facts, set(matched))
        support = ", ".join(f"{incident} ({facts_per_incident[incident]} facts)" for incident in matched[:3])
        if shared:
            return f"Strong: the signature `{shared[0][:120]}` recurs in {support}."
        return f"Strong: Hindsight matched the confirmed cause in {support}."
    if strength == "weak":
        thin = [incident for incident in matched if facts_per_incident[incident] < strong_match_min_facts]
        if thin:
            return (
                f"Weak: {', '.join(thin[:3])} looked similar but had fewer than {strong_match_min_facts} supporting "
                "facts, so treat it as a lead, not a known fix."
            )
        return "Weak: past incidents look related, but none share this exact error signature or a confirmed cause."
    if not facts:
        return (
            "Novel: Hindsight retrieved no memories for these signatures; this appears to be a new problem."
        )
    return (
        f"Novel: Hindsight retrieved {len(facts)} memories, but none share these error signatures or a confirmed "
        "cause, so PagerMind abstains instead of guessing."
    )
