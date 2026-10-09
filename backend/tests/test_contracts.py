"""Contract tests: our gateways against stubbed Hindsight and Groq SDK responses.

These pin down how SDK response shapes are interpreted (citations, zero counts,
idempotent retains, model fallback) without network access.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from app.core.config import Settings
from app.core.errors import DependencyUnavailable
from app.domain.recommendation import BaselineAnswer
from app.llm.groq_client import GroqClient
from app.memory.client import HindsightGateway


def _settings() -> Settings:
    return Settings(_env_file=None, hindsight_api_key="test", groq_api_key="test")  # type: ignore[call-arg]


class StubHindsight:
    def __init__(self) -> None:
        self.retain_calls: list[dict[str, Any]] = []
        self.operation_metadata: dict[str, Any] = {}
        self.operations = SimpleNamespace(get_operation_status=self._status)

    async def areflect(self, **_: Any) -> Any:
        memories = [
            SimpleNamespace(
                id="f1",
                text="pool exhausted",
                document_id="incident:INC-2025",
                metadata=None,
                occurred_start="2026-01-10",
                tags=["service:payments-api"],
            ),
            SimpleNamespace(
                id="f2",
                text="feedback",
                document_id="feedback:INC-L1",
                metadata={"incident_id": "INC-L1"},
                occurred_start=None,
                tags=None,
            ),
            SimpleNamespace(
                id="f3", text="runbook", document_id="runbook:RB-1", metadata={}, occurred_start=None, tags=[]
            ),
        ]
        return SimpleNamespace(
            text="answer",
            structured_output={"summary": "s"},
            structured_output_error=None,
            based_on=SimpleNamespace(memories=memories),
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )

    async def aretain_batch(self, **kwargs: Any) -> Any:
        self.retain_calls.append(kwargs)
        return SimpleNamespace(operation_id="op-123", operation_ids=None)

    async def _status(self, bank_id: str, operation_id: str) -> Any:
        return SimpleNamespace(
            operation_id=operation_id,
            status="completed",
            error_message=None,
            result_metadata=self.operation_metadata,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def hindsight() -> tuple[HindsightGateway, StubHindsight]:
    gateway = HindsightGateway(_settings(), bank_id="bank")
    stub = StubHindsight()
    gateway._client = stub  # type: ignore[assignment]
    return gateway, stub


async def test_reflect_maps_facts_to_incidents(hindsight: tuple[HindsightGateway, StubHindsight]) -> None:
    gateway, _ = hindsight
    result = await gateway.reflect(query="q", context="c", response_schema={})
    assert [fact.incident_id for fact in result.facts] == ["INC-2025", "INC-L1", None]
    assert result.structured == {"summary": "s"}
    assert (result.input_tokens, result.output_tokens) == (11, 7)


async def test_retain_is_async_and_idempotent(hindsight: tuple[HindsightGateway, StubHindsight]) -> None:
    gateway, stub = hindsight
    operation = await gateway.submit_retain(
        document_id="incident:INC-1", item={"content": "x"}, operation_id="uuid"
    )
    assert operation == "op-123"
    call = stub.retain_calls[0]
    assert (
        call["retain_async"] is True
        and call["operation_id"] == "uuid"
        and call["document_id"] == "incident:INC-1"
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"memory_unit_count": 0, "items_count": 1}, 0),
        ({"memory_unit_count": 9}, 9),
        ({"items_count": 1}, None),
    ],
)
async def test_operation_status_preserves_zero_and_ignores_item_count(
    hindsight: tuple[HindsightGateway, StubHindsight], metadata: dict[str, Any], expected: int | None
) -> None:
    gateway, stub = hindsight
    stub.operation_metadata = metadata
    status = await gateway.operation_status("op-1")
    assert status.memory_unit_count == expected


class StubCompletions:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.models: list[str] = []

    async def create(self, *, model: str, **kwargs: Any) -> Any:
        self.models.append(model)
        response = self.responses[model]
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )


def _groq(responses: dict[str, Any]) -> tuple[GroqClient, StubCompletions]:
    client = GroqClient(_settings())
    completions = StubCompletions(responses)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))  # type: ignore[assignment]
    return client, completions


async def test_groq_falls_back_to_the_secondary_model() -> None:
    settings = _settings()
    client, completions = _groq(
        {
            settings.groq_primary_model: "not json",
            settings.groq_fallback_model: json.dumps({"summary": "ok"}),
        }
    )
    result = await client.complete_json(name="t", system="s", user="u", schema=BaselineAnswer)
    assert result.value.summary == "ok"
    assert result.fallback_used and result.model == settings.groq_fallback_model
    assert completions.models == [settings.groq_primary_model, settings.groq_fallback_model]


async def test_groq_raises_when_every_model_fails() -> None:
    settings = _settings()
    client, _ = _groq({settings.groq_primary_model: "{}", settings.groq_fallback_model: "[]"})
    with pytest.raises(DependencyUnavailable):
        await client.complete_json(name="t", system="s", user="u", schema=BaselineAnswer)


async def test_reflect_times_out_once_and_is_never_retried() -> None:
    """A slow reflect must fall back to the baseline after one budget-specific timeout, not wait twice."""
    settings = _settings().model_copy(
        update={"hindsight_reflect_timeout_seconds": 0.05, "hindsight_reflect_high_timeout_seconds": 0.2}
    )
    gateway = HindsightGateway(settings, bank_id="bank")
    calls: list[float] = []

    class Slow(StubHindsight):
        async def areflect(self, **_: Any) -> Any:
            calls.append(time.perf_counter())
            await asyncio.sleep(1)

    gateway._client = Slow()  # type: ignore[assignment]
    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        await gateway.reflect(query="q", context="c", response_schema={}, budget="mid")
    assert len(calls) == 1 and time.perf_counter() - started < 0.5


async def test_reflect_returns_mental_models_as_consolidated_evidence(
    hindsight: tuple[HindsightGateway, StubHindsight],
) -> None:
    gateway, stub = hindsight
    original = stub.areflect

    async def with_models(**kwargs: Any) -> Any:
        response = await original(**kwargs)
        response.based_on.mental_models = [SimpleNamespace(id="mm-1", text="INC-2025 pattern", context=None)]
        return response

    stub.areflect = with_models  # type: ignore[method-assign]
    result = await gateway.reflect(query="q", context="c", response_schema={})
    assert [(m.fact_id, m.fact_type, m.incident_id) for m in result.mental_models] == [
        ("mm-1", "mental_model", None)
    ]


class ConfigStub(StubHindsight):
    def __init__(self, defense_status: int | None, not_entitled: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.defense_status = defense_status
        self.not_entitled = not_entitled
        self.policy: dict[str, Any] | None = None
        self.directives_synced = False

    async def acreate_bank(self, **_: Any) -> None:
        return None

    async def aupdate_bank_config(self, bank_id: str, **updates: Any) -> None:
        policy = updates.get("memory_defense")
        if policy is None:
            return
        refused = {rule["on"] for rule in policy["rules"]} & self.not_entitled
        status = self.defense_status or (400 if refused else None)
        if status:
            raise aiohttp.ClientResponseError(request_info=None, history=(), status=status)  # type: ignore[arg-type]
        self.policy = policy

    async def alist_directives(self, **_: Any) -> Any:
        self.directives_synced = True
        return SimpleNamespace(items=[])

    async def acreate_directive(self, **_: Any) -> None:
        return None


@pytest.mark.parametrize(
    ("status", "not_entitled", "active"),
    [
        (None, frozenset(), ["sensitive_data", "prompt_injection"]),
        (None, frozenset({"prompt_injection"}), ["sensitive_data"]),  # what the live plan returned
        (403, frozenset(), []),
    ],
)
async def test_memory_defense_keeps_every_entitled_detector(
    status: int | None, not_entitled: frozenset[str], active: list[str]
) -> None:
    from app.memory.bank import MEMORY_DEFENSE, BankConfig

    gateway = HindsightGateway(_settings(), bank_id="bank")
    stub = ConfigStub(status, not_entitled)
    gateway._client = stub  # type: ignore[assignment]
    assert (
        await gateway.apply_bank_config(BankConfig(bank_id="bank"), memory_defense=MEMORY_DEFENSE) == active
    )
    if active:
        assert stub.policy is not None and [rule["on"] for rule in stub.policy["rules"]] == active
    assert stub.directives_synced  # the rest of the configuration is still applied


async def test_malformed_memory_defense_policy_fails_loudly() -> None:
    from app.memory.bank import MEMORY_DEFENSE, BankConfig

    gateway = HindsightGateway(_settings(), bank_id="bank")
    gateway._client = ConfigStub(422)  # type: ignore[assignment]
    with pytest.raises(aiohttp.ClientResponseError):
        await gateway.apply_bank_config(BankConfig(bank_id="bank"), memory_defense=MEMORY_DEFENSE)


def test_memory_defense_rules_use_the_documented_shape() -> None:
    from app.memory.bank import MEMORY_DEFENSE

    detectors = {"sensitive_data", "prompt_injection", "detect_secrets", "eu_pii", "llm_screen"}
    for rule in MEMORY_DEFENSE["rules"]:
        assert (
            set(rule) == {"on", "action"}
            and rule["on"] in detectors
            and rule["action"] in {"redact", "block"}
        )
