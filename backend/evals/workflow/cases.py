"""Schema for the workflow evaluation dataset (data/evaluation/workflow_cases.jsonl).

A case is one conversation of one or more turns. Each turn describes:

* inputs: an optional message and optional attachments (with ownership / tampering scenarios)
* simulation: how the external services behave for this turn in offline mode
  (what Groq extracts, what Hindsight reflect answers, which dependency is down)
* expect: what the workflow must do (offline, scripted externals)
* expect_live: model-agnostic expectations used when running against real Groq/Hindsight;
  absent when the turn depends on scripted model behaviour

The dataset tests the *workflow* (routing, guardrails, evidence validation, safety,
resilience, persistence), not answer quality, which `pagermind-eval run` measures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "data" / "evaluation" / "workflow_cases.jsonl"

Outcome = Literal["answered", "clarification", "rejected", "memory_unavailable", "error", "upload_only"]
Strength = Literal["strong", "weak", "none", "unavailable"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Attachment(Strict):
    name: str
    content: str
    owner: Literal["self", "other_conversation", "other_engineer"] = "self"
    tamper: bool = False


class FactSpec(Strict):
    incident_id: str
    count: int = Field(ge=1)


class HindsightScript(Strict):
    mode: Literal["answer", "error"] = "answer"
    answer: dict[str, Any] | None = None
    facts: list[FactSpec] = Field(default_factory=list)


class Simulation(Strict):
    understanding: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    hindsight: HindsightScript = Field(default_factory=HindsightScript)
    groq_fail: list[Literal["understand_incident", "baseline_without_memory"]] = Field(default_factory=list)


class Expectation(Strict):
    outcome: Outcome | list[Outcome] | None = None
    error_code: str | None = None
    match_strength: Strength | list[Strength] | None = None
    memory_available: bool | None = None
    matched_incidents: list[str] | None = None
    guardrail_reasons: list[str] = Field(default_factory=list)
    forbidden_strings: list[str] = Field(default_factory=list)
    any_requires_approval: bool | None = None
    escalation_team: str | None = None
    all_steps_hypothesis: bool | None = None
    abstention: bool | None = None
    avoid_excludes: list[str] = Field(default_factory=list)
    summary_excludes: list[str] = Field(default_factory=list)
    summary_contains: list[str] = Field(default_factory=list)
    max_steps: int | None = None
    incident_opened: bool | None = None
    recent_changes_include: list[str] = Field(default_factory=list)
    recent_changes_exclude: list[str] = Field(default_factory=list)
    error_signatures_min: int | None = None
    upload_status: list[int] | None = None
    upload_reason: str | None = None
    upload_redactions_min: int | None = None


class Turn(Strict):
    message: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    simulation: Simulation = Field(default_factory=Simulation)
    expect: Expectation
    expect_live: Expectation | None = None


class DeploymentSeed(Strict):
    deployment_id: str
    service_id: str
    hours_ago: float
    version: str = "v1.0.0"
    change_summary: str = "Evaluation fixture"


class WorkflowCase(Strict):
    case_id: str = Field(pattern=r"^WF-\d{4}$")
    category: str
    description: str
    engineer_id: str = "priya"
    service_id: str | None = None
    deployments: list[DeploymentSeed] = Field(default_factory=list)
    turns: list[Turn] = Field(min_length=1)

    @property
    def runs_live(self) -> bool:
        return all(turn.expect_live is not None for turn in self.turns)


def load_cases(
    path: Path = DEFAULT_PATH, *, categories: set[str] | None = None, limit: int | None = None
) -> list[WorkflowCase]:
    cases = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = WorkflowCase.model_validate_json(line)
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: invalid case: {error}") from error
        if categories and not any(
            case.category == c or case.category.startswith(f"{c}.") for c in categories
        ):
            continue
        cases.append(case)
    return cases[:limit] if limit else cases


def dump_cases(cases: list[WorkflowCase], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(case.model_dump(exclude_defaults=True), sort_keys=True, ensure_ascii=False)
        for case in cases
    ]
    path.write_text("\n".join(lines) + "\n")
