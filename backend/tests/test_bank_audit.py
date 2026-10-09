from __future__ import annotations

from app.memory.audit import audit_bank
from app.memory.bank import DIRECTIVES
from app.memory.client import StoredDocument

SCOPED = ["environment:production", "service:payments-api"]


def test_audit_finds_legacy_unscoped_empty_and_missing_documents() -> None:
    documents = [
        StoredDocument("incident:INC-2025", SCOPED, 12),
        StoredDocument("INC-2025", ["service:payments-api"], 9),  # retired seed script
        StoredDocument("runbook:RB-payments-api", ["memory_type:procedure"], 4),  # pre-fix tags
        StoredDocument("feedback:INC-L1", SCOPED, 0),
    ]
    report = audit_bank(
        documents,
        expected_document_ids={"incident:INC-2025", "incident:INC-2030", "runbook:RB-payments-api"},
        directive_names=[],
        webhook_registered=False,
        config={"memory_defense": True},
    )
    assert report.legacy_documents == ["INC-2025"]
    assert report.out_of_scope == ["runbook:RB-payments-api"]
    assert report.empty_documents == ["feedback:INC-L1"]
    assert report.missing_documents == ["incident:INC-2030"]
    assert report.missing_directives == [name for name, _ in DIRECTIVES]
    assert len(report.problems) == 6


def test_a_healthy_bank_has_no_problems() -> None:
    report = audit_bank(
        [StoredDocument("incident:INC-2025", SCOPED, 12)],
        expected_document_ids={"incident:INC-2025"},
        directive_names=[name for name, _ in DIRECTIVES],
        webhook_registered=True,
        config={"memory_defense": True, "observations": True},
    )
    assert report.problems == []
