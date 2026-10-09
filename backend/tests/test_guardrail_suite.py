from __future__ import annotations

from collections import Counter
from pathlib import Path

from evals.guardrail_suite import run_suite


def test_all_guardrail_cases_pass(data_dir: Path) -> None:
    results = run_suite(data_dir)
    failures = [r for r in results if not r.passed]
    assert len(results) == 200
    assert not failures, Counter(r.category for r in failures)
