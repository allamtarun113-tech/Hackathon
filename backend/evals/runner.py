"""Run every scenario in both modes and score each response.

Modes
    without_memory  plain Groq answer from the alert and logs (the UI's baseline)
    with_memory     the production grounded path (`app.agent.grounding`): the same reflect query, context,
                    production tag scope, severity budget and recent changes, then the same evidence and
                    safety guardrails, against a disposable bank holding production's retain documents
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from app.agent import grounding, prompts, understanding
from app.domain.recommendation import BaselineAnswer, Recommendation
from app.guardrails.input_guard import neutralise_injection, redact_sensitive
from app.guardrails.output_guard import is_destructive, validate_safety
from app.llm.groq_client import GroqClient
from app.memory.client import HindsightGateway
from evals.dataset import Scenario
from evals.judge import Judge

Mode = Literal["without_memory", "with_memory"]


@dataclass
class ResultRow:
    case_id: str
    category: str
    mode: Mode
    repetition: int
    schema_valid: bool
    retrieved_incident_ids: list[str] = field(default_factory=list)
    predicted_strong_match: bool | None = None
    abstained: bool | None = None
    recall_at_3: float | None = None
    precision_at_3: float | None = None
    citation_valid: bool | None = None
    unsafe_unflagged: bool = False
    unsafe_raw: bool = False
    root_cause: float | None = None
    recommended_action: float | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    response: dict[str, Any] | None = None
    error: str | None = None
    winner: str | None = None
    memory_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceContext:
    """The reference data production reads from Supabase, loaded from the same seed files."""

    deployments: list[dict[str, Any]]
    depends_on: dict[str, list[str]]
    teams: dict[str, str]

    def recent_changes(
        self, service_id: str, fired_at: str | None, window: timedelta = timedelta(hours=24), limit: int = 10
    ) -> list[dict[str, Any]]:
        """Same rows as `deployments.recent_changes`: the service and its dependencies, 24h before the alert."""
        if not fired_at:
            return []
        before = datetime.fromisoformat(fired_at)
        related = {service_id, *self.depends_on.get(service_id, [])}
        rows = [
            {
                "deployment_id": deploy["id"],
                "service_id": deploy["service"],
                "version": deploy.get("version"),
                "previous_version": deploy.get("previous_version"),
                "change_type": deploy.get("type", "deploy"),
                "change_summary": deploy.get("change_summary"),
                "deployed_by": deploy.get("deployed_by"),
                "deployed_at": deploy["at"],
            }
            for deploy in self.deployments
            if deploy["service"] in related
            and before - window <= datetime.fromisoformat(deploy["at"]) <= before
        ]
        rows.sort(key=lambda row: datetime.fromisoformat(row["deployed_at"]), reverse=True)
        return rows[:limit]


def top_k_incidents(
    recommendation: Recommendation, facts_per_incident: Counter[str], k: int = 3
) -> list[str]:
    ranked = [m.incident_id for m in recommendation.matched_incidents]
    ranked += [incident for incident, _ in facts_per_incident.most_common() if incident not in ranked]
    return ranked[:k]


def retrieval_scores(top3: list[str], expected: list[str]) -> tuple[float | None, float | None]:
    expected_set = set(expected)
    hits = len(set(top3) & expected_set)
    recall = hits / min(3, len(expected_set)) if expected_set else None
    # No predictions is perfectly precise only when nothing was expected (a correct abstention).
    precision = hits / len(top3) if top3 else float(not expected_set)
    return recall, precision


class EvaluationRunner:
    def __init__(
        self,
        *,
        hindsight: HindsightGateway,
        groq: GroqClient,
        judge: Judge,
        known_incident_ids: set[str],
        strong_match_min_facts: int,
        context: ServiceContext,
        concurrency: int = 4,
        seed: int = 7,
    ) -> None:
        self.context = context
        self.services = set(context.teams)
        self.hindsight = hindsight
        self.groq = groq
        self.judge = judge
        self.known = known_incident_ids
        self.strong_match_min_facts = strong_match_min_facts
        self._semaphore = asyncio.Semaphore(concurrency)
        self._rng = random.Random(seed)  # noqa: S311 - only randomises answer order for the judge

    async def run(self, scenarios: list[Scenario], repetitions: int) -> list[ResultRow]:
        tasks = [self._run_pair(scenario, rep) for scenario in scenarios for rep in range(1, repetitions + 1)]
        rows: list[ResultRow] = []
        for pair in await asyncio.gather(*tasks):
            rows.extend(pair)
        return rows

    async def _run_pair(self, scenario: Scenario, repetition: int) -> tuple[ResultRow, ResultRow]:
        async with self._semaphore:
            without, with_memory = await asyncio.gather(
                self._without_memory(scenario, repetition),
                self._with_memory(scenario, repetition),
            )
            for row in (without, with_memory):
                if row.response is not None:
                    try:
                        score = await self.judge.score(scenario.labels, row.response)
                        row.root_cause, row.recommended_action = score.root_cause, score.recommended_action
                    except Exception as error:
                        row.error = row.error or f"judge: {type(error).__name__}"
            if without.response is not None and with_memory.response is not None:
                try:
                    winner = await self.judge.compare(
                        scenario.labels, with_memory.response, without.response, self._rng
                    )
                    without.winner = with_memory.winner = winner
                except Exception as error:
                    with_memory.error = with_memory.error or f"pairwise: {type(error).__name__}"
            return without, with_memory

    # ------------------------------------------------------------------ modes
    async def _without_memory(self, scenario: Scenario, repetition: int) -> ResultRow:
        row = ResultRow(
            scenario.input.case_id, scenario.labels.category, "without_memory", repetition, schema_valid=False
        )
        started = time.perf_counter()
        try:
            result = await self.groq.complete_json(
                name="eval_baseline",
                system=prompts.BASELINE_SYSTEM,
                user=prompts.baseline_prompt(message=scenario.input.as_message(), log_text=""),
                schema=BaselineAnswer,
            )
        except Exception as error:
            row.error = type(error).__name__
            return row
        row.latency_ms = int((time.perf_counter() - started) * 1000)
        row.schema_valid = True
        row.response = result.value.model_dump()
        row.input_tokens, row.output_tokens = result.input_tokens or 0, result.output_tokens or 0
        row.predicted_strong_match = False
        row.abstained = False
        steps = result.value.recommended_steps
        row.unsafe_raw = any(is_destructive(f"{s.action} {s.command or ''}") for s in steps)
        row.unsafe_unflagged = row.unsafe_raw  # the baseline has no output guard
        return row

    async def _with_memory(self, scenario: Scenario, repetition: int) -> ResultRow:
        row = ResultRow(
            scenario.input.case_id, scenario.labels.category, "with_memory", repetition, schema_valid=False
        )
        # Production redacts and neutralises logs (redact_sensitive_data) before understanding and reflect.
        log_text = neutralise_injection(redact_sensitive("\n".join(scenario.input.log_excerpt)).text).text
        message = scenario.input.alert_message()
        started = time.perf_counter()
        # Same understanding step as production. If the model call fails, production continues with the
        # deterministic signatures alone, and so does the evaluation.
        try:
            model = (
                await understanding.ask_model(
                    self.groq, message=message, log_text=log_text, known_services=self.services, prior={}
                )
            ).value
        except Exception:
            model = None
        understood = understanding.resolve(
            model, message=message, log_text=log_text, known_services=self.services, prior={}
        )
        try:
            reflected = await grounding.grounded_reflect(
                self.hindsight,
                # The alert names its service; production asks for clarification when it cannot tell.
                service_id=understood.service_id or scenario.input.service_id,
                severity=understood.severity or scenario.input.severity,
                symptoms=understood.symptoms,
                error_signatures=understood.error_signatures,
                recent_changes=self.context.recent_changes(
                    scenario.input.service_id, scenario.input.fired_at
                ),
                log_text=log_text,
            )
        except Exception as error:
            # In production this turn would silently degrade to the baseline; count it, never hide it.
            row.error = type(error).__name__
            row.memory_failed = True
            return row
        row.latency_ms = int((time.perf_counter() - started) * 1000)
        row.input_tokens, row.output_tokens = reflected.input_tokens or 0, reflected.output_tokens or 0

        recommendation, parse_check = grounding.parse_reflect(reflected.structured, reflected.text)
        row.schema_valid = parse_check is None
        if parse_check is not None:
            row.error = "invalid_structured_output"

        facts = grounding.evidence_dicts(reflected)
        row.unsafe_raw = any(
            is_destructive(f"{s.action} {s.command or ''}") for s in recommendation.recommended_steps
        )
        retrieved = grounding.incident_support(facts)[1]
        row.citation_valid = recommendation.cited_ids <= (retrieved & self.known)

        grounded = grounding.ground(
            recommendation,
            facts=facts,
            known_incident_ids=self.known,
            strong_match_min_facts=self.strong_match_min_facts,
        )
        team = self.context.teams.get(scenario.input.service_id)
        final = validate_safety(grounded.recommendation, escalation_team=team).recommendation

        top3 = top_k_incidents(final, grounded.facts_per_incident)
        row.retrieved_incident_ids = top3
        row.recall_at_3, row.precision_at_3 = retrieval_scores(top3, scenario.labels.expected_incident_ids)
        row.predicted_strong_match = final.is_strong_match
        row.abstained = not final.is_strong_match
        row.unsafe_unflagged = any(
            is_destructive(f"{s.action} {s.command or ''}") and not s.requires_approval
            for s in final.recommended_steps
        )
        row.response = final.model_dump()
        return row
