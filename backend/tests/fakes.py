"""In-memory stand-ins for Hindsight, Groq and Supabase Storage.

They implement the same async surface as the real gateways so the API, agent and
worker run end to end against a real Postgres without network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.core.errors import DependencyUnavailable
from app.domain.recommendation import BaselineAnswer, IncidentUnderstanding
from app.llm.groq_client import LLMResult
from app.memory.client import BankStats, MemoryFact, OperationStatus, ReflectResult


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_uploads = False

    async def upload(
        self, bucket: str, path: str, data: bytes, *, content_type: str, upsert: bool = True
    ) -> None:
        if self.fail_uploads:
            raise DependencyUnavailable("storage down")
        self.objects[(bucket, path)] = data

    async def download(self, bucket: str, path: str) -> bytes:
        if (bucket, path) not in self.objects:
            raise DependencyUnavailable("missing object")
        return self.objects[(bucket, path)]

    async def delete(self, bucket: str, path: str) -> None:
        self.objects.pop((bucket, path), None)

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None


def strong_match_answer(*, cite: list[str]) -> dict[str, Any]:
    return {
        "summary": f"HikariCP pool exhaustion caused by the reconciliation job, as in {cite[0]}.",
        "is_strong_match": True,
        "matched_incidents": [
            {"incident_id": incident, "why_similar": "Same HikariCP timeout"} for incident in cite
        ],
        "similarity_explanation": "Identical connection-pool timeout signature.",
        "likely_cause": "recon-job holds Postgres connections during peak traffic",
        "confidence": 0.86,
        "recommended_steps": [
            {
                "order": 1,
                "action": "Suspend the recon-job cronjob",
                "rationale": "Freed the pool within four minutes previously",
                "command": 'kubectl -n payments patch cronjob recon-job -p \'{"spec":{"suspend":true}}\'',
                "evidence_ids": cite,
                "is_hypothesis": False,
            }
        ],
        "actions_to_avoid": [
            {"action": "Restart payments-api pods", "reason": "Did not help before", "evidence_ids": cite[:1]}
        ],
        "escalation_team": "Payments",
        "abstention_reason": "",
    }


@dataclass
class FakeHindsight:
    bank_id: str = "pagermind-test"
    structured: dict[str, Any] | None = None
    facts: list[MemoryFact] = field(default_factory=list)
    reflect_error: Exception | None = None
    operations: dict[str, OperationStatus] = field(default_factory=dict)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    submit_error: Exception | None = None
    reflect_calls: list[dict[str, Any]] = field(default_factory=list)

    async def ping(self) -> str:
        return "test"

    async def bank_stats(self, *, refresh: bool = False) -> BankStats:
        return BankStats(
            documents=1, observations=0, pending_operations=0, pending_consolidation=0, failed_consolidation=0
        )

    async def reflect(self, **kwargs: Any) -> ReflectResult:
        self.reflect_calls.append(kwargs)
        if self.reflect_error:
            raise self.reflect_error
        return ReflectResult(
            structured=self.structured,
            structured_error=None,
            text="reflect text",
            facts=self.facts,
            input_tokens=100,
            output_tokens=50,
        )

    async def submit_retain(self, *, document_id: str, item: dict[str, Any], operation_id: str) -> str:
        if self.submit_error:
            raise self.submit_error
        self.submitted.append({"document_id": document_id, "item": item, "operation_id": operation_id})
        return operation_id

    async def operation_status(self, operation_id: str) -> OperationStatus:
        if operation_id not in self.operations:
            raise DependencyUnavailable("unknown operation")
        return self.operations[operation_id]

    def complete(self, operation_id: str, units: int | None) -> None:
        self.operations[operation_id] = OperationStatus(operation_id, "completed", None, units)

    async def close(self) -> None:
        return None


def fact(
    incident_id: str, text: str = "HikariPool-1 connection timeout resolved by suspending recon-job"
) -> MemoryFact:
    return MemoryFact(
        fact_id=f"fact-{incident_id}-{len(text)}",
        text=text,
        document_id=f"incident:{incident_id}",
        incident_id=incident_id,
        occurred_at="2026-01-10T02:40:00Z",
        tags=["environment:production", "memory_type:incident"],
        memory_type="incident",
    )


@dataclass
class FakeGroq:
    """Returns canned answers keyed by the call name used in the code."""

    understanding: IncidentUnderstanding = field(
        default_factory=lambda: IncidentUnderstanding(
            service_id="payments-api",
            severity="SEV1",
            symptoms="p99 latency 10s and 5xx at 13%",
            error_signatures=["HikariPool-1 - Connection is not available, request timed out after 30000ms."],
        )
    )
    baseline: BaselineAnswer = field(
        default_factory=lambda: BaselineAnswer(
            summary="Database connections look exhausted.",
            likely_cause="Connection pool saturation",
            recommended_steps=[{"order": 1, "action": "Check the connection pool metrics"}],  # type: ignore[list-item]
        )
    )
    fail: set[str] = field(default_factory=set)
    models: list[str] = field(default_factory=lambda: ["fake-primary", "fake-fallback"])

    async def complete_json(self, *, name: str, schema: type[BaseModel], **_: Any) -> LLMResult[Any]:
        if name in self.fail:
            raise DependencyUnavailable(f"groq failed for {name}")
        value: BaseModel = self.understanding if schema is IncidentUnderstanding else self.baseline
        return LLMResult(
            value=value,
            model="fake-primary",
            fallback_used=False,
            input_tokens=10,
            output_tokens=5,
            latency_ms=1,
        )

    async def close(self) -> None:
        return None
