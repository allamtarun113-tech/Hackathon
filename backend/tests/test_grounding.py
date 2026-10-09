from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent import grounding
from app.domain.recommendation import MatchedIncident, Recommendation
from app.importers.source import SeedSource
from app.memory.bank import REFLECT_SCOPE_TAGS
from app.memory.client import _memory_type
from app.memory.documents import build_feedback_document, build_runbook_document
from evals.bank import production_documents
from evals.runner import ServiceContext
from tests.fakes import FakeHindsight, fact

SIGNATURE = "HikariPool-1 - Connection is not available, request timed out after 30000ms."


def _in_scope(tags: list[str]) -> bool:
    return bool(set(REFLECT_SCOPE_TAGS) & set(tags))


def test_every_memory_type_is_reachable_by_reflect(data_dir: Path) -> None:
    """Live reflect filters on REFLECT_SCOPE_TAGS; a document outside it can never ground an answer."""
    documents = production_documents(SeedSource(data_dir))
    documents.append(
        build_feedback_document(
            feedback_id="FDB-1",
            incident_id="INC-L1",
            service_id="payments-api",
            engineer_name="Asha",
            step_action="Suspend recon-job",
            outcome="worked",
            note="pool recovered",
            occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    assert {document.memory_type for document in documents} == {"incident", "procedure", "feedback"}
    unreachable = [document.document_id for document in documents if not _in_scope(document.item["tags"])]
    assert unreachable == []


def test_runbooks_are_tagged_with_service_and_environment() -> None:
    document = build_runbook_document(
        runbook_id="RB-payments-api",
        service_id="payments-api",
        title="Payments",
        version=2,
        markdown="# Payments",
        approved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert {"environment:production", "service:payments-api", "memory_type:procedure"} <= set(
        document.item["tags"]
    )


def test_evaluation_bank_excludes_held_out_cases(data_dir: Path) -> None:
    source = SeedSource(data_dir)
    sources = {document.source_id for document in production_documents(source)}
    assert not sources & {scenario["id"] for scenario in source.scenarios}


def test_live_reflect_uses_production_scope_and_severity_budget() -> None:
    hindsight = FakeHindsight()

    async def _call(severity: str) -> dict[str, Any]:
        await grounding.grounded_reflect(
            hindsight,  # type: ignore[arg-type]
            service_id="payments-api",
            severity=severity,
            symptoms="p99 10s",
            error_signatures=[SIGNATURE],
            recent_changes=[],
            log_text="line\n" * 200,
        )
        return hindsight.reflect_calls[-1]

    sev1, sev3 = asyncio.run(_call("SEV1")), asyncio.run(_call("SEV3"))
    assert sev1["tags"] == REFLECT_SCOPE_TAGS == sev3["tags"]
    assert (sev1["budget"], sev3["budget"]) == ("high", "mid")
    assert "service:" not in " ".join(sev1["tags"])  # cross-service signature matches stay possible


def test_memory_summary_counts_memories_incidents_and_observations() -> None:
    facts = [
        grounding.fact_to_dict(fact("INC-1")),
        grounding.fact_to_dict(fact("INC-1", "other")),
        grounding.fact_to_dict(fact("INC-2")),
    ]
    facts.append(
        {"text": "restarts never fix pool exhaustion", "fact_type": "observation", "incident_id": None}
    )
    summary = grounding.memory_summary(facts)
    assert (summary["memories"], summary["incidents"], summary["observations"]) == (4, 2, 1)
    assert summary["by_memory_type"]["incident"] == 3


def test_match_explanation_cites_shared_signature() -> None:
    facts = [grounding.fact_to_dict(fact("INC-2025", f"Exact error signatures: {SIGNATURE}"))]
    rec = Recommendation(
        summary="s",
        is_strong_match=True,
        matched_incidents=[MatchedIncident(incident_id="INC-2025", why_similar="same")],
        confidence=0.9,
    )
    text = grounding.match_explanation(
        "strong",
        recommendation=rec,
        facts=facts,
        error_signatures=[SIGNATURE],
        facts_per_incident=Counter({"INC-2025": 3}),
        strong_match_min_facts=2,
    )
    assert text.startswith("Strong:") and "HikariPool-1" in text and "INC-2025 (3 facts)" in text
    novel = grounding.match_explanation(
        "none",
        recommendation=Recommendation(summary="s", is_strong_match=False, confidence=0.1),
        facts=[],
        error_signatures=[SIGNATURE],
        facts_per_incident=Counter(),
        strong_match_min_facts=2,
    )
    assert novel.startswith("Novel:")


def test_memory_type_falls_back_to_document_prefix() -> None:
    assert _memory_type(["memory_type:feedback"], "incident:INC-1") == "feedback"
    assert _memory_type([], "runbook:RB-payments-api") == "procedure"
    assert _memory_type([], None) is None


def test_eval_recent_changes_mirror_production_window() -> None:
    context = ServiceContext(
        deployments=[
            {"id": "DEP-1", "service": "payments-api", "at": "2026-09-29T01:00:00+05:30"},
            {"id": "DEP-2", "service": "postgres", "at": "2026-09-29T02:00:00+05:30"},
            {"id": "DEP-3", "service": "payments-api", "at": "2026-09-20T02:00:00+05:30"},
            {"id": "DEP-4", "service": "search-service", "at": "2026-09-29T02:00:00+05:30"},
        ],
        depends_on={"payments-api": ["postgres"]},
        teams={},
    )
    rows = context.recent_changes("payments-api", "2026-09-29T02:44:00+05:30")
    assert [row["deployment_id"] for row in rows] == ["DEP-2", "DEP-1"]


def test_incident_documents_carry_signatures_as_entities(data_dir: Path) -> None:
    incident = SeedSource(data_dir).incidents[0]
    document = next(d for d in production_documents(SeedSource(data_dir)) if d.source_id == incident["id"])
    entities = document.item["entities"]
    assert {"text": incident["id"], "type": "incident"} in entities
    assert {"text": incident["service"], "type": "service"} in entities
    signatures = [e["text"] for e in entities if e["type"] == "error_signature"]
    assert signatures and all(signature in document.item["content"] for signature in signatures)


def test_memory_guard_scans_entities(data_dir: Path) -> None:
    from app.core.hashing import payload_checksum
    from app.guardrails.memory_guard import validate_memory_payload

    document = production_documents(SeedSource(data_dir))[0]
    payload = document.payload()
    payload["item"]["entities"].append({"text": "expected_incident_ids: INC-2001 (EVAL-001)", "type": "x"})
    assert validate_memory_payload(payload, expected_checksum=payload_checksum(payload)).reason == (
        "evaluation_data_detected"
    )


def test_feedback_marks_retrieval_but_adds_no_strong_match_support() -> None:
    postmortem = grounding.fact_to_dict(fact("INC-L1"))
    feedback = {**postmortem, "fact_id": "f2", "memory_type": "feedback", "document_id": "feedback:INC-L1"}
    only_feedback = {**feedback, "incident_id": "INC-L2", "document_id": "feedback:INC-L2"}
    support, surfaced = grounding.incident_support([postmortem, feedback, only_feedback])
    assert support == Counter({"INC-L1": 1})
    assert surfaced == {"INC-L1", "INC-L2"}


def test_observations_and_mental_models_can_support_citations() -> None:
    facts = [
        {
            "text": "Restarting pods never fixed pool exhaustion (INC-2025, INC-2031).",
            "fact_type": "observation",
        },
        {"text": "payments-api: INC-2025 recon-job pattern", "fact_type": "mental_model"},
    ]
    support, surfaced = grounding.incident_support(facts)
    assert surfaced == {"INC-2025", "INC-2031"} and support["INC-2025"] == 2


def test_reflect_query_is_search_signal_and_rules_live_in_context() -> None:
    from app.agent import prompts

    query = prompts.reflect_query(service_id="payments-api", symptoms="p99 10s", error_signatures=[SIGNATURE])
    context = prompts.reflect_context(recent_changes=[], log_excerpt="")
    assert SIGNATURE in query and len(query) < 400 and "Rules" not in query
    assert prompts.REFLECT_RULES in context


def test_evaluation_bank_waits_for_consolidation() -> None:
    from app.memory.client import BankStats
    from evals.bank import ConsolidationIncomplete, wait_for_consolidation

    class Gateway:
        bank_id = "eval"

        def __init__(self, pending: list[int]) -> None:
            self.pending = pending

        async def bank_stats(self, *, refresh: bool = False) -> BankStats:
            value = self.pending.pop(0) if len(self.pending) > 1 else self.pending[0]
            return BankStats(
                documents=1,
                observations=0,
                pending_operations=0,
                pending_consolidation=value,
                failed_consolidation=0,
            )

    drained = Gateway([5, 2, 0])
    asyncio.run(wait_for_consolidation(drained, poll=0))  # type: ignore[arg-type]
    assert drained.pending == [0]
    import pytest

    with pytest.raises(ConsolidationIncomplete):
        asyncio.run(wait_for_consolidation(Gateway([3]), limit_seconds=0, poll=0))  # type: ignore[arg-type]


def test_error_signature_label_accepts_several_signatures_per_fact() -> None:
    from app.memory.bank import entity_labels

    labels = {label.key: label.to_api() for label in entity_labels(["payments-api"], ["Payments"])}
    assert labels["error_signature"] == {"key": "error_signature", "type": "multi-text", "tag": False}
    assert labels["failure_domain"]["type"] == "text"
    assert labels["service"]["type"] == "value"


def test_memory_failures_fail_the_evaluation_gate() -> None:
    from evals.metrics import check_targets

    summary = {"with_memory": {"memory_failure_rate": 0.2}, "with_memory_win_rate": 0.9}
    assert check_targets(summary)["memory_failure_rate"]["passed"] is False


def test_evaluation_loader_waits_and_reports_empty_or_failed_retains(data_dir: Path) -> None:
    import pytest

    from app.memory.client import OperationStatus
    from evals.bank import RetainFailed, load_documents

    documents = production_documents(SeedSource(data_dir))[:3]

    class Gateway:
        bank_id = "eval"

        def __init__(self, final: dict[str, tuple[str, int | None]]) -> None:
            self.final, self.polls = final, 0

        async def submit_batch(self, items: list[dict[str, Any]]) -> list[str]:
            return [item["document_id"] for item in items]  # one operation per item

        async def operation_status(self, operation_id: str) -> OperationStatus:
            self.polls += 1
            status, units = self.final[operation_id] if self.polls > 3 else ("processing", None)
            return OperationStatus(operation_id, status, None, units)

    ok = {d.document_id: ("completed", 5) for d in documents}
    ok[documents[1].document_id] = ("completed", 0)
    result = asyncio.run(load_documents(Gateway(ok), documents, poll=0))  # type: ignore[arg-type]
    assert (result.documents, result.operations, result.empty) == (3, 3, [documents[1].document_id])

    failing = {**ok, documents[2].document_id: ("failed", None)}
    with pytest.raises(RetainFailed):
        asyncio.run(load_documents(Gateway(failing), documents, poll=0))  # type: ignore[arg-type]
