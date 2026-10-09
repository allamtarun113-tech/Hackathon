"""Scripted stand-ins for Groq, Hindsight and Storage used in offline workflow evaluation.

They implement the same async surface as the production gateways. Each turn sets a
`Simulation` on them before the chat request is sent, so the real FastAPI app, LangGraph
workflow, guardrails and Postgres persistence run unchanged around them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.errors import DependencyUnavailable
from app.domain.recommendation import BaselineAnswer, IncidentUnderstanding
from app.llm.groq_client import LLMResult
from app.memory.client import MemoryFact, OperationStatus, ReflectResult
from evals.workflow.cases import Simulation

DEFAULT_BASELINE = {
    "summary": "The service appears degraded; start with the error-rate and saturation dashboards.",
    "likely_cause": "Unknown without history",
    "recommended_steps": [{"order": 1, "action": "Check the service dashboards for saturation"}],
    "actions_to_avoid": [],
}


class MemoryStorage:
    """In-memory Supabase Storage. Used in both modes: storage is infrastructure, not what is evaluated."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def upload(
        self, bucket: str, path: str, data: bytes, *, content_type: str, upsert: bool = True
    ) -> None:
        self.objects[(bucket, path)] = data

    async def download(self, bucket: str, path: str) -> bytes:
        if (bucket, path) not in self.objects:
            raise DependencyUnavailable("Object not found.")
        return self.objects[(bucket, path)]

    async def delete(self, bucket: str, path: str) -> None:
        self.objects.pop((bucket, path), None)

    def tamper(self, path_suffix: str) -> bool:
        for key, data in self.objects.items():
            if key[1].endswith(path_suffix):
                self.objects[key] = data[::-1] + b"#tampered"
                return True
        return False

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None


class ScriptedGroq:
    def __init__(self) -> None:
        self.models = ["scripted-groq"]
        self.simulation = Simulation()

    async def complete_json(self, *, name: str, schema: type[BaseModel], **_: Any) -> LLMResult[Any]:
        if name in self.simulation.groq_fail:
            raise DependencyUnavailable(f"Scripted Groq outage for {name}.")
        if schema is IncidentUnderstanding:
            value: BaseModel = IncidentUnderstanding.model_validate(self.simulation.understanding or {})
        else:
            value = BaselineAnswer.model_validate(self.simulation.baseline or DEFAULT_BASELINE)
        return LLMResult(
            value=value,
            model="scripted-groq",
            fallback_used=False,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
        )

    async def close(self) -> None:
        return None


class ScriptedHindsight:
    bank_id = "workflow-eval"

    def __init__(self) -> None:
        self.simulation = Simulation()

    async def ping(self) -> str:
        return "scripted"

    async def reflect(self, **_: Any) -> ReflectResult:
        script = self.simulation.hindsight
        if script.mode == "error":
            raise DependencyUnavailable("Scripted Hindsight outage.")
        facts = [
            MemoryFact(
                fact_id=f"{spec.incident_id}-fact-{n}",
                text=f"{spec.incident_id}: confirmed resolution fact {n + 1}",
                document_id=f"incident:{spec.incident_id}",
                incident_id=spec.incident_id,
                occurred_at="2026-01-01T00:00:00Z",
            )
            for spec in script.facts
            for n in range(spec.count)
        ]
        return ReflectResult(
            structured=script.answer,
            structured_error=None,
            text="Scripted reflect text.",
            facts=facts,
            input_tokens=0,
            output_tokens=0,
        )

    async def operation_status(self, operation_id: str) -> OperationStatus:
        raise DependencyUnavailable("Not used by the workflow evaluation.")

    async def close(self) -> None:
        return None
