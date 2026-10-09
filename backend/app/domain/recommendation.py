"""Structured contracts for the agent: what Hindsight reflect must return and what the API emits."""

from __future__ import annotations

from typing import Any, Literal

from annotated_types import Ge, Le, MaxLen
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MatchStrength = Literal["strong", "weak", "none", "unavailable"]


class LenientModel(BaseModel):
    """Base for model-generated output.

    LLM output is untrusted in shape as well as content. Values that exceed a declared limit
    (a ninth step, an over-long summary, confidence 1.2) are trimmed to fit instead of causing
    the whole grounded answer to be discarded. Missing or wrongly-typed fields still fail.
    """

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _fit_to_limits(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fitted = dict(data)
        for name, field in cls.model_fields.items():
            value = fitted.get(name)
            if value is None or isinstance(value, bool):
                continue
            for constraint in field.metadata:
                if isinstance(constraint, MaxLen) and isinstance(value, str | list):
                    value = value[: constraint.max_length]
                elif isinstance(constraint, Ge) and isinstance(value, int | float):
                    value = max(value, constraint.ge)  # type: ignore[call-overload]
                elif isinstance(constraint, Le) and isinstance(value, int | float):
                    value = min(value, constraint.le)  # type: ignore[call-overload]
            fitted[name] = value
        return fitted


class MatchedIncident(LenientModel):
    incident_id: str = Field(pattern=r"^INC-[A-Za-z0-9-]+$")
    why_similar: str = Field(max_length=600)


class RecommendedStep(LenientModel):
    step_id: str = ""
    order: int = Field(ge=1, le=20)
    action: str = Field(min_length=3, max_length=500)
    rationale: str = Field(default="", max_length=800)
    command: str | None = Field(default=None, max_length=600)
    evidence_ids: list[str] = Field(default_factory=list)
    is_hypothesis: bool = False
    requires_approval: bool = False
    warning: str | None = None

    @field_validator("command")
    @classmethod
    def _blank_command_is_none(cls, value: str | None) -> str | None:
        if value is None or value.strip().lower() in {"", "n/a", "na", "none", "null", "-"}:
            return None
        return value.strip()


class ActionToAvoid(LenientModel):
    action: str = Field(min_length=3, max_length=400)
    reason: str = Field(default="", max_length=600)
    evidence_ids: list[str] = Field(default_factory=list)


class Recommendation(LenientModel):
    """The single answer shown to the engineer. Produced by Hindsight reflect, then validated."""

    summary: str = Field(min_length=1, max_length=1200)
    is_strong_match: bool
    matched_incidents: list[MatchedIncident] = Field(default_factory=list, max_length=5)
    similarity_explanation: str = Field(default="", max_length=1200)
    likely_cause: str = Field(default="", max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_steps: list[RecommendedStep] = Field(default_factory=list, max_length=8)
    actions_to_avoid: list[ActionToAvoid] = Field(default_factory=list, max_length=8)
    escalation_team: str | None = None
    abstention_reason: str | None = None

    @property
    def cited_ids(self) -> set[str]:
        ids = {match.incident_id for match in self.matched_incidents}
        for step in self.recommended_steps:
            ids.update(step.evidence_ids)
        for avoid in self.actions_to_avoid:
            ids.update(avoid.evidence_ids)
        return ids


def reflect_response_schema() -> dict[str, Any]:
    """JSON Schema passed to Hindsight reflect. Kept explicit (not model_json_schema) so it stays provider-friendly."""
    string = {"type": "string"}
    ids = {"type": "array", "items": string}
    return {
        "type": "object",
        "properties": {
            "summary": string,
            "is_strong_match": {"type": "boolean"},
            "matched_incidents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"incident_id": string, "why_similar": string},
                    "required": ["incident_id", "why_similar"],
                },
            },
            "similarity_explanation": string,
            "likely_cause": string,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "recommended_steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "order": {"type": "integer"},
                        "action": string,
                        "rationale": string,
                        "command": string,
                        "evidence_ids": ids,
                        "is_hypothesis": {"type": "boolean"},
                    },
                    "required": ["order", "action", "rationale", "evidence_ids", "is_hypothesis"],
                },
            },
            "actions_to_avoid": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"action": string, "reason": string, "evidence_ids": ids},
                    "required": ["action", "reason", "evidence_ids"],
                },
            },
            "escalation_team": string,
            "abstention_reason": string,
        },
        "required": [
            "summary",
            "is_strong_match",
            "matched_incidents",
            "likely_cause",
            "confidence",
            "recommended_steps",
            "actions_to_avoid",
        ],
    }


class IncidentUnderstanding(LenientModel):
    """Extraction produced by the `understand_incident` node."""

    service_id: str | None = None
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"] | None = None
    symptoms: str = ""
    error_signatures: list[str] = Field(default_factory=list, max_length=8)
    customer_impact: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None


class BaselineAnswer(LenientModel):
    """The 'without memory' answer: a plain LLM call that sees only the alert and logs."""

    summary: str
    likely_cause: str = ""
    recommended_steps: list[RecommendedStep] = Field(default_factory=list, max_length=8)
    actions_to_avoid: list[ActionToAvoid] = Field(default_factory=list, max_length=8)
