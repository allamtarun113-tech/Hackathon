"""Shared guardrail result types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

Phase = Literal["input", "memory", "output", "runtime"]


class Decision(StrEnum):
    ALLOW = "allow"
    REDACT_AND_CONTINUE = "redact_and_continue"
    SANITIZE_AND_CONTINUE = "sanitize_and_continue"
    TRUNCATE_AND_CONTINUE = "truncate_and_continue"
    REJECT = "reject"
    REJECT_OUTPUT = "reject_output"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    REPAIR_OUTPUT = "repair_output"


@dataclass(frozen=True)
class GuardrailCheck:
    phase: Phase
    check_name: str
    decision: Decision
    reason_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["decision"] = self.decision.value
        return record
