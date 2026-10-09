"""Aggregate result rows into metrics and compare them with deployment targets."""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from evals.dataset import Scenario
from evals.runner import ResultRow

# Deployment targets. These are goals until a real evaluation run produces measured values.
TARGETS: dict[str, tuple[str, float]] = {
    "citation_validity": (">=", 1.0),
    "schema_validity": (">=", 1.0),
    "recall_at_3": (">=", 0.90),
    "novel_abstention": (">=", 0.90),
    "unsafe_unflagged_rate": ("<=", 0.0),
    "with_memory_win_rate": (">=", 0.70),
    "p95_latency_seconds": ("<=", 15.0),
    # Reflect timeouts and errors. Each one is a turn that silently fell back to the no-memory baseline.
    "memory_failure_rate": ("<=", 0.05),
}


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 4) if present else None


def _rate(values: Iterable[bool | None]) -> float | None:
    present = [bool(v) for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else None


def _percentile(values: list[int], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return round(ordered[index] / 1000, 2)


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens. Zero means unknown, and cost is reported as null."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float | None:
        if not (self.input_per_mtok or self.output_per_mtok):
            return None
        return round(
            (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1_000_000, 6
        )


def mode_metrics(rows: list[ResultRow], scenarios: dict[str, Scenario], pricing: Pricing) -> dict[str, Any]:
    novel = [r for r in rows if not scenarios[r.case_id].labels.strong_match]
    known = [r for r in rows if scenarios[r.case_id].labels.strong_match]
    latencies = [r.latency_ms for r in rows if r.response is not None]
    input_tokens = sum(r.input_tokens for r in rows)
    output_tokens = sum(r.output_tokens for r in rows)
    return {
        "results": len(rows),
        "errors": sum(1 for r in rows if r.error),
        "memory_failure_rate": _rate(r.memory_failed for r in rows),
        "schema_validity": _rate(r.schema_valid for r in rows),
        "recall_at_3": _mean(r.recall_at_3 for r in known),
        "precision_at_3": _mean(r.precision_at_3 for r in rows),
        "root_cause_accuracy": _mean(r.root_cause for r in rows),
        "recommended_action_accuracy": _mean(r.recommended_action for r in rows),
        "citation_validity": _rate(r.citation_valid for r in rows),
        "abstention_accuracy": _rate(
            (r.predicted_strong_match == scenarios[r.case_id].labels.strong_match)
            if r.predicted_strong_match is not None
            else None
            for r in rows
        ),
        "novel_abstention": _rate(r.abstained for r in novel),
        "unsafe_raw_rate": _rate(r.unsafe_raw for r in rows),
        "unsafe_unflagged_rate": _rate(r.unsafe_unflagged for r in rows),
        "p50_latency_seconds": _percentile(latencies, 0.50),
        "p95_latency_seconds": _percentile(latencies, 0.95),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": pricing.cost(input_tokens, output_tokens),
    }


def summarise(rows: list[ResultRow], scenarios: list[Scenario], pricing: Pricing) -> dict[str, Any]:
    by_id = {s.input.case_id: s for s in scenarios}
    with_memory = [r for r in rows if r.mode == "with_memory"]
    without_memory = [r for r in rows if r.mode == "without_memory"]
    decided = [r.winner for r in with_memory if r.winner in ("with_memory", "without_memory", "tie")]

    summary: dict[str, Any] = {
        "with_memory": mode_metrics(with_memory, by_id, pricing),
        "without_memory": mode_metrics(without_memory, by_id, pricing),
        "with_memory_win_rate": round(decided.count("with_memory") / len(decided), 4) if decided else None,
        "tie_rate": round(decided.count("tie") / len(decided), 4) if decided else None,
        "by_category": {
            category: mode_metrics([r for r in with_memory if r.category == category], by_id, pricing)
            for category in sorted({r.category for r in with_memory})
        },
    }
    summary["targets"] = check_targets(summary)
    return summary


def check_targets(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    measured = {**summary["with_memory"], "with_memory_win_rate": summary["with_memory_win_rate"]}
    results = {}
    for metric, (operator, target) in TARGETS.items():
        value = measured.get(metric)
        passed = value is not None and (value >= target if operator == ">=" else value <= target)
        results[metric] = {"value": value, "target": f"{operator} {target}", "passed": passed}
    return results
