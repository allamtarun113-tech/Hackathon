"""The workflow evaluation dataset is valid, complete and reproducible."""

from __future__ import annotations

from collections import Counter

from evals.workflow.cases import DEFAULT_PATH, dump_cases, load_cases
from evals.workflow.generate import generate

REQUIRED_CATEGORIES = {
    "routing.answered", "routing.off_topic", "routing.clarification",
    "input.secrets", "input.pii", "input.injection",
    "attachments.valid", "attachments.foreign", "attachments.tampered", "attachments.bad_file",
    "evidence.invented_citation", "evidence.not_retrieved", "evidence.weak_reported_strong", "evidence.novel",
    "safety.destructive_command", "safety.execution_claim", "safety.contradiction",
    "resilience.memory_down", "resilience.understanding_down", "resilience.all_down",
    "resilience.invalid_output", "resilience.oversized_output", "context.recent_changes",
}  # fmt: skip


def test_committed_dataset_loads_and_covers_every_category() -> None:
    cases = load_cases(DEFAULT_PATH)
    counts = Counter(case.category for case in cases)
    assert set(counts) == REQUIRED_CATEGORIES
    assert all(count >= 5 for count in counts.values())
    assert len({case.case_id for case in cases}) == len(cases)


def test_dataset_is_reproducible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fresh = tmp_path / "cases.jsonl"
    dump_cases(generate(), fresh)
    assert fresh.read_text() == DEFAULT_PATH.read_text(), "Run `pagermind-eval workflow generate`."


def test_every_live_case_has_model_agnostic_expectations() -> None:
    for case in load_cases(DEFAULT_PATH):
        for turn in case.turns:
            live = turn.expect_live
            if live is None:
                continue
            # Live expectations must not depend on scripted model output.
            assert live.matched_incidents is None and live.match_strength is None, case.case_id
