"""Compare what is actually in a Hindsight bank with what PagerMind expects to be there.

Pure functions over data read from the bank, so the rules are unit-tested; `pagermind-bank audit`
does the reading. Nothing here writes to Hindsight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.memory.bank import DIRECTIVES, REFLECT_SCOPE_TAGS
from app.memory.client import StoredDocument

# Every document PagerMind writes has one of these ID prefixes (see app.memory.documents).
KNOWN_PREFIXES = ("incident:", "feedback:", "runbook:")


@dataclass
class BankAudit:
    documents: int = 0
    legacy_documents: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    empty_documents: list[str] = field(default_factory=list)
    missing_documents: list[str] = field(default_factory=list)
    missing_directives: list[str] = field(default_factory=list)
    webhook_registered: bool = False
    config: dict[str, bool] = field(default_factory=dict)

    @property
    def problems(self) -> list[str]:
        found = []
        if self.legacy_documents:
            found.append(f"{len(self.legacy_documents)} legacy documents not written by PagerMind")
        if self.out_of_scope:
            found.append(f"{len(self.out_of_scope)} documents outside reflect's tag scope (re-import them)")
        if self.empty_documents:
            found.append(f"{len(self.empty_documents)} documents with zero memory units (not searchable)")
        if self.missing_documents:
            found.append(f"{len(self.missing_documents)} expected documents missing (run the importer)")
        if self.missing_directives:
            found.append(f"directives missing: {', '.join(self.missing_directives)} (run `make bank`)")
        if not self.webhook_registered:
            found.append("retain webhook not registered (run `make bank`)")
        found += [f"config missing: {name}" for name, ok in self.config.items() if not ok]
        return found

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "problems": self.problems}


def audit_bank(
    documents: list[StoredDocument],
    *,
    expected_document_ids: set[str],
    directive_names: list[str],
    webhook_registered: bool,
    config: dict[str, bool],
) -> BankAudit:
    ours = [d for d in documents if d.document_id.startswith(KNOWN_PREFIXES)]
    stored = {d.document_id for d in documents}
    return BankAudit(
        documents=len(documents),
        legacy_documents=sorted(
            d.document_id for d in documents if not d.document_id.startswith(KNOWN_PREFIXES)
        ),
        out_of_scope=sorted(d.document_id for d in ours if not set(REFLECT_SCOPE_TAGS) & set(d.tags)),
        empty_documents=sorted(d.document_id for d in ours if d.memory_unit_count == 0),
        missing_documents=sorted(expected_document_ids - stored),
        missing_directives=[name for name, _ in DIRECTIVES if name not in set(directive_names)],
        webhook_registered=webhook_registered,
        config=config,
    )
