"""Turning an engineer's report into the reflect inputs (service, severity, symptoms, signatures).

Shared by the live graph and the evaluation runner: the signatures and symptoms Hindsight is queried
with are the main driver of recall, so both must derive them identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent import prompts
from app.domain.recommendation import IncidentUnderstanding
from app.llm.groq_client import GroqClient, LLMResult
from app.memory.signatures import error_signatures

MAX_SIGNATURES = 6


@dataclass(frozen=True)
class Understanding:
    service_id: str | None
    severity: str | None
    symptoms: str
    error_signatures: list[str]
    clarification_question: str | None
    missing_context: bool


async def ask_model(
    groq: GroqClient, *, message: str, log_text: str, known_services: set[str], prior: dict[str, Any]
) -> LLMResult[IncidentUnderstanding]:
    return await groq.complete_json(
        name="understand_incident",
        system=prompts.UNDERSTAND_SYSTEM,
        user=prompts.understand_prompt(
            message=message, log_text=log_text, known_services=sorted(known_services), prior=prior
        ),
        schema=IncidentUnderstanding,
        max_tokens=800,
    )


def service_from_text(text: str, known: set[str]) -> str | None:
    lowered = text.lower()
    matches = [service for service in known if service in lowered]
    return max(matches, key=len) if matches else None


def resolve(
    model: IncidentUnderstanding | None,
    *,
    message: str,
    log_text: str,
    known_services: set[str],
    prior: dict[str, Any],
) -> Understanding:
    """Merge deterministic log signatures, the model's extraction and earlier turns (model may be None)."""
    model = model or IncidentUnderstanding(symptoms=message[:500])
    deterministic = error_signatures([*log_text.splitlines(), *message.splitlines()])
    service_id = model.service_id if model.service_id in known_services else None
    service_id = service_id or prior.get("service_id") or service_from_text(message, known_services)
    signatures = list(
        dict.fromkeys([*deterministic, *model.error_signatures, *prior.get("error_signatures", [])])
    )[:MAX_SIGNATURES]
    symptoms = model.symptoms or prior.get("symptoms") or message[:500]
    return Understanding(
        service_id=service_id,
        severity=model.severity or prior.get("severity"),
        symptoms=symptoms,
        error_signatures=signatures,
        clarification_question=model.clarification_question,
        missing_context=service_id is None or (not signatures and len(symptoms) < 20),
    )
