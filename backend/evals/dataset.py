"""Held-out scenarios, split into what the system may see and what only the grader may see."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScenarioInput:
    """Everything the system under test is allowed to receive."""

    case_id: str
    service_id: str
    severity: str
    alert_title: str
    alert_value: str
    log_excerpt: list[str]
    fired_at: str | None = None

    def alert_message(self) -> str:
        """The alert as an engineer would report it; logs travel separately, as an attachment would."""
        return f"{self.service_id} alert: {self.alert_title} ({self.alert_value}). Severity {self.severity}."

    def as_message(self) -> str:
        return (
            f"{self.service_id} alert: {self.alert_title} ({self.alert_value}). Severity {self.severity}.\n"
            "Recent log lines:\n" + "\n".join(self.log_excerpt[-40:])
        )


@dataclass(frozen=True)
class ScenarioLabels:
    """Grader-only labels. Never sent to Hindsight or to the system under test."""

    category: str
    strong_match: bool
    likely_cause: str
    should_recommend: list[str]
    should_avoid: list[str]
    expected_incident_ids: list[str]
    expected_escalation: str | None


@dataclass(frozen=True)
class Scenario:
    input: ScenarioInput
    labels: ScenarioLabels


def load_scenarios(
    data_dir: Path, *, limit: int | None = None, categories: set[str] | None = None
) -> list[Scenario]:
    raw: list[dict[str, Any]] = json.loads((data_dir / "demo_scenarios.json").read_text())
    scenarios = []
    for item in raw:
        expected = item["expected"]
        category = item.get("case_type", "unknown")
        if categories and category not in categories:
            continue
        scenarios.append(
            Scenario(
                input=ScenarioInput(
                    case_id=item["id"],
                    service_id=item["service"],
                    severity=item["severity"],
                    alert_title=item["alert"]["title"],
                    alert_value=item["alert"].get("value", ""),
                    log_excerpt=item.get("log_excerpt", []),
                    fired_at=item["alert"].get("fired_at"),
                ),
                labels=ScenarioLabels(
                    category=category,
                    strong_match=bool(expected["strong_match"]),
                    likely_cause=expected.get("likely_cause", ""),
                    should_recommend=list(expected.get("should_recommend", [])),
                    should_avoid=list(expected.get("should_avoid", [])),
                    expected_incident_ids=list(expected.get("expected_incident_ids", [])),
                    expected_escalation=expected.get("expected_escalation"),
                ),
            )
        )
    return scenarios[:limit] if limit else scenarios
