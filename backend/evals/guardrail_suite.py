"""Run the 200 guardrail cases against the production guardrail functions (no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.guardrails.input_guard import guard_input
from app.guardrails.output_guard import check_output_text

MAX_MESSAGE_CHARS = 4_000


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    passed: bool
    expected: dict[str, Any]
    observed: dict[str, Any]


def evaluate_case(
    case: dict[str, Any], *, known_incident_ids: set[str], known_services: set[str]
) -> CaseResult:
    expected = case["expected"]
    if case["phase"] == "input":
        result = guard_input(case["input"], max_chars=MAX_MESSAGE_CHARS, known_services=known_services)
        decisions = {check.decision.value for check in result.checks}
        reasons = set(result.reason_codes)
        leaked = [value for value in expected.get("redact_values", []) if value in result.text]
        passed = expected["decision"] in decisions and expected["reason_code"] in reasons and not leaked
        observed = {"decisions": sorted(decisions), "reasons": sorted(reasons), "leaked": leaked}
    else:
        checks = check_output_text(case["input"], known_incident_ids=known_incident_ids)
        decisions = {check.decision.value for check in checks}
        reasons = {check.reason_code for check in checks if check.reason_code}
        passed = expected["decision"] in decisions and expected["reason_code"] in reasons
        observed = {"decisions": sorted(decisions), "reasons": sorted(reasons)}
    return CaseResult(case["guardrail_test_id"], case["category"], passed, expected, observed)


def run_suite(data_dir: Path) -> list[CaseResult]:
    cases = json.loads((data_dir / "production" / "guardrail_tests.json").read_text())
    incidents = json.loads((data_dir / "incidents_history.json").read_text())
    services = set(yaml.safe_load((data_dir / "service_catalog.yaml").read_text())["services"])
    known = {incident["id"] for incident in incidents}
    return [evaluate_case(case, known_incident_ids=known, known_services=services) for case in cases]
