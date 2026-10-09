"""Aggregate workflow evaluation results into metrics, a JSON report and a Markdown summary."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.workflow.runner import CaseResult

LEAK_CHECKS = {"no_leak", "no_secret_in_output"}


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct * (len(ordered) - 1)))]


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    by_category: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        by_category[result.category].append(result)

    check_totals: Counter[str] = Counter()
    check_passed: Counter[str] = Counter()
    guard_expected: Counter[str] = Counter()
    guard_found: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    latencies: list[int] = []
    leaks = invariant_failures = 0

    for result in results:
        for turn in result.turns:
            if turn.expected_outcome:
                confusion[turn.expected_outcome[0] if len(turn.expected_outcome) == 1 else "any-answer"][
                    turn.outcome
                ] += 1
            if turn.latency_ms:
                latencies.append(turn.latency_ms)
            for check in turn.checks:
                check_totals[check.name] += 1
                check_passed[check.name] += int(check.passed)
                if check.name.startswith("guardrail:"):
                    reason = check.name.split(":", 1)[1]
                    guard_expected[reason] += 1
                    guard_found[reason] += int(check.passed)
                if not check.passed and check.name in LEAK_CHECKS:
                    leaks += 1
                if not check.passed and check.invariant:
                    invariant_failures += 1

    passed = sum(r.passed for r in results)
    return {
        "cases": len(results),
        "passed": passed,
        "pass_rate": _rate(passed, len(results)),
        "crashed_cases": sum(1 for r in results if r.error),
        "leaks": leaks,
        "invariant_failures": invariant_failures,
        "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
        "by_category": {
            category: {
                "cases": len(items),
                "passed": sum(r.passed for r in items),
                "pass_rate": _rate(sum(r.passed for r in items), len(items)),
            }
            for category, items in sorted(by_category.items())
        },
        "checks": {
            name: {
                "total": check_totals[name],
                "passed": check_passed[name],
                "rate": _rate(check_passed[name], check_totals[name]),
            }
            for name in sorted(check_totals)
        },
        "guardrail_recall": {
            reason: _rate(guard_found[reason], guard_expected[reason]) for reason in sorted(guard_expected)
        },
        "routing_confusion": {expected: dict(actual) for expected, actual in sorted(confusion.items())},
    }


def failures(results: list[CaseResult], limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        if result.passed:
            continue
        if result.error:
            rows.append(
                {
                    "case_id": result.case_id,
                    "category": result.category,
                    "check": "crash",
                    "detail": result.error,
                }
            )
        for turn in result.turns:
            for check in turn.checks:
                if not check.passed:
                    rows.append(
                        {
                            "case_id": result.case_id,
                            "category": result.category,
                            "turn": turn.index + 1,
                            "check": check.name,
                            "detail": check.detail,
                        }
                    )
    return rows[:limit]


def write_reports(
    results: list[CaseResult], summary: dict[str, Any], *, mode: str, directory: Path
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    json_path = directory / f"workflow-{mode}-{stamp}.json"
    md_path = directory / f"workflow-{mode}-{stamp}.md"
    json_path.write_text(
        json.dumps(
            {
                "mode": mode,
                "summary": summary,
                "failures": failures(results, 500),
                "results": [asdict(r) for r in results],
            },
            indent=2,
            default=str,
        )
    )
    md_path.write_text(markdown(summary, failures(results), mode=mode))
    return json_path, md_path


def markdown(summary: dict[str, Any], failed: list[dict[str, Any]], *, mode: str) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    lines = [
        f"# Workflow evaluation ({mode})",
        "",
        f"**{summary['passed']} / {summary['cases']} cases passed ({pct(summary['pass_rate'])})** · "
        f"leaks: {summary['leaks']} · invariant failures: {summary['invariant_failures']} · "
        f"crashes: {summary['crashed_cases']} · latency p50 {summary['latency_ms']['p50']} ms, "
        f"p95 {summary['latency_ms']['p95']} ms",
        "",
        "## By category",
        "",
        "| Category | Cases | Passed | Rate |",
        "|---|---:|---:|---:|",
        *[
            f"| {name} | {row['cases']} | {row['passed']} | {pct(row['pass_rate'])} |"
            for name, row in summary["by_category"].items()
        ],
        "",
        "## Checks",
        "",
        "| Check | Passed / total | Rate |",
        "|---|---:|---:|",
        *[
            f"| {name} | {row['passed']} / {row['total']} | {pct(row['rate'])} |"
            for name, row in summary["checks"].items()
        ],
        "",
        "## Guardrail detection recall",
        "",
        "| Reason code | Recall |",
        "|---|---:|",
        *[f"| {reason} | {pct(rate)} |" for reason, rate in summary["guardrail_recall"].items()],
        "",
        "## Routing (expected → actual)",
        "",
        *[
            f"- **{expected}** → " + ", ".join(f"{actual}: {count}" for actual, count in row.items())
            for expected, row in summary["routing_confusion"].items()
        ],
        "",
    ]
    if failed:
        lines += ["## Failures", "", "| Case | Category | Check | Detail |", "|---|---|---|---|"]
        lines += [
            f"| {f['case_id']} | {f['category']} | {f['check']} | {str(f.get('detail', '')).replace('|', '/')[:160]} |"
            for f in failed
        ]
    return "\n".join(lines) + "\n"
