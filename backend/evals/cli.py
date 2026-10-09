"""Evaluation CLI.

pagermind-eval guardrails                      # 200 guardrail cases, offline
pagermind-eval probe                           # live check of Hindsight on a small disposable bank
pagermind-eval run --repetitions 5             # 100 scenarios x 2 modes x 5 runs = 1,000 results
pagermind-eval run --limit 10 --repetitions 1  # quick smoke evaluation
pagermind-eval workflow generate               # workflow test dataset (data/evaluation/)
pagermind-eval workflow run                    # workflow evaluation: routing, guardrails, evidence, safety
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer

from app.core.config import BACKEND_ROOT, Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.pool import Database
from app.db.repositories import evaluation
from app.importers.source import SeedSource
from app.llm.groq_client import GroqClient
from evals.bank import build_evaluation_bank
from evals.dataset import load_scenarios
from evals.guardrail_suite import run_suite
from evals.judge import Judge
from evals.metrics import Pricing, summarise
from evals.runner import EvaluationRunner, ResultRow, ServiceContext
from evals.workflow.cli import app as workflow_app

app = typer.Typer(help="PagerMind evaluations.", no_args_is_help=True)
app.add_typer(workflow_app, name="workflow")
log = get_logger(__name__)
REPORT_DIR = BACKEND_ROOT / "evals" / "reports"


def _commit_sha() -> str:
    sha = os.getenv("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        return subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], text=True).strip()  # noqa: S607
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _write_report(run_id: str, payload: dict[str, object]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{run_id}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


@app.command()
def guardrails(fail_under: float = typer.Option(1.0, help="Minimum pass rate required.")) -> None:
    """Run the guardrail suite and exit non-zero if the pass rate is below the threshold."""
    results = run_suite(get_settings().data_dir)
    passed = sum(r.passed for r in results)
    by_category = Counter(r.category for r in results if not r.passed)
    typer.echo(f"Guardrail suite: {passed}/{len(results)} passed")
    for category, failures in sorted(by_category.items()):
        typer.echo(f"  {category}: {failures} failing")
    _write_report(f"guardrails-{datetime.now(UTC):%Y%m%dT%H%M%S}", {"results": [asdict(r) for r in results]})
    if passed / len(results) < fail_under:
        raise typer.Exit(code=1)


async def _persist(
    settings: Settings,
    run_id: str,
    rows: list[ResultRow],
    summary: dict[str, object],
    meta: dict[str, object],
    pricing: Pricing,
) -> None:
    db = Database(settings)
    await db.open()
    try:
        async with db.transaction() as conn:
            await evaluation.start_run(conn, {"evaluation_run_id": run_id, **meta})
            for row in rows:
                await evaluation.insert_result(
                    conn,
                    {
                        "evaluation_result_id": f"{run_id}-{row.case_id}-{row.mode}-{row.repetition}",
                        "evaluation_run_id": run_id,
                        "case_id": row.case_id,
                        "mode": row.mode,
                        "repetition": row.repetition,
                        "retrieved_incident_ids": row.retrieved_incident_ids,
                        "predicted_strong_match": row.predicted_strong_match,
                        "abstained": row.abstained,
                        "schema_valid": row.schema_valid,
                        "scores": {
                            "recall_at_3": row.recall_at_3,
                            "precision_at_3": row.precision_at_3,
                            "root_cause": row.root_cause,
                            "recommended_action": row.recommended_action,
                            "citation_valid": row.citation_valid,
                            "unsafe_unflagged": row.unsafe_unflagged,
                            "winner": row.winner,
                            "memory_failed": row.memory_failed,
                        },
                        "response": row.response,
                        "latency_ms": row.latency_ms,
                        "input_tokens": row.input_tokens,
                        "output_tokens": row.output_tokens,
                        "cost_usd": pricing.cost(row.input_tokens, row.output_tokens),
                        "error": row.error,
                    },
                )
            await evaluation.finish_run(conn, run_id, status="succeeded", summary=summary)
    finally:
        await db.close()


def _service_context(settings: Settings) -> ServiceContext:
    source = SeedSource(settings.data_dir)
    services = source.catalog["services"]
    return ServiceContext(
        deployments=source.deployments,
        depends_on={
            service_id: list(service.get("depends_on", [])) for service_id, service in services.items()
        },
        teams={service_id: service["team"] for service_id, service in services.items()},
    )


async def _run(
    *,
    repetitions: int,
    limit: int | None,
    categories: set[str] | None,
    judge_model: str,
    concurrency: int,
    keep_bank: bool,
    persist: bool,
    pricing: Pricing,
) -> dict[str, object]:
    settings = get_settings()
    run_id = f"EVAL-RUN-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    bank_id = f"pagermind-eval-{uuid.uuid4().hex[:10]}"
    if bank_id == settings.hindsight_bank_id:
        raise RuntimeError("Refusing to evaluate against the production bank.")

    scenarios = load_scenarios(settings.data_dir, limit=limit, categories=categories)
    groq = GroqClient(settings)
    judge_groq = GroqClient(settings.model_copy(update={"groq_primary_model": judge_model}))
    log.info(
        "evaluation_started",
        run_id=run_id,
        bank_id=bank_id,
        scenarios=len(scenarios),
        repetitions=repetitions,
    )

    gateway, known_ids = await build_evaluation_bank(settings, bank_id)
    try:
        runner = EvaluationRunner(
            hindsight=gateway,
            groq=groq,
            judge=Judge(judge_groq),
            known_incident_ids=known_ids,
            strong_match_min_facts=settings.strong_match_min_facts,
            context=_service_context(settings),
            concurrency=concurrency,
        )
        rows = await runner.run(scenarios, repetitions)
    finally:
        if not keep_bank:
            await gateway.delete_bank()
        await gateway.close()
        await groq.close()
        await judge_groq.close()

    summary = summarise(rows, scenarios, pricing)
    meta = {
        "commit_sha": _commit_sha(),
        "bank_id": bank_id,
        "chat_model": f"{settings.groq_primary_model}+hindsight",
        "judge_model": judge_model,
        "repetitions": repetitions,
    }
    report = {
        "run_id": run_id,
        "provenance": "live_measurement",
        **meta,
        "summary": summary,
        "results": [row.to_dict() for row in rows],
    }
    path = _write_report(run_id, report)
    log.info("evaluation_report_written", path=str(path))
    if persist:
        await _persist(settings, run_id, rows, summary, meta, pricing)
    return summary


@app.command()
def run(
    repetitions: int = typer.Option(5, min=1),
    limit: int | None = typer.Option(None, help="Only the first N scenarios."),
    category: list[str] | None = typer.Option(None, help="Restrict to case categories (repeatable)."),
    judge_model: str = typer.Option("qwen/qwen3-32b"),
    concurrency: int = typer.Option(4, min=1, max=16),
    keep_bank: bool = typer.Option(False, help="Keep the disposable bank for debugging."),
    persist: bool = typer.Option(True, help="Write runs and results to Supabase."),
    enforce_targets: bool = typer.Option(False, help="Exit non-zero if any deployment target is missed."),
    input_price_per_mtok: float = typer.Option(0.0, help="USD per 1M input tokens (0 = unknown)."),
    output_price_per_mtok: float = typer.Option(0.0, help="USD per 1M output tokens (0 = unknown)."),
) -> None:
    """Measure with- vs without-memory quality on the held-out scenarios."""
    configure_logging(get_settings().log_level, json_output=False)
    summary = asyncio.run(
        _run(
            repetitions=repetitions,
            limit=limit,
            categories=set(category) if category else None,
            judge_model=judge_model,
            concurrency=concurrency,
            keep_bank=keep_bank,
            persist=persist,
            pricing=Pricing(input_price_per_mtok, output_price_per_mtok),
        )
    )
    targets = summary["targets"]
    assert isinstance(targets, dict)
    for metric, result in targets.items():
        mark = "PASS" if result["passed"] else "FAIL"
        typer.echo(f"{mark:4}  {metric:24} {result['value']!s:>8}  target {result['target']}")
    if enforce_targets and not all(result["passed"] for result in targets.values()):
        raise typer.Exit(code=1)


@app.command()
def probe(
    case: list[str] | None = typer.Option(None, help="Scenario IDs to reflect on (repeatable)."),
    keep_bank: bool = typer.Option(False, help="Keep the probe bank for inspection."),
) -> None:
    """Verify config, entities, tags, observations and reflect latency against live Hindsight."""
    from evals.probe import run_probe

    configure_logging(get_settings().log_level, json_output=False)
    settings = get_settings()
    cases = case or _default_probe_cases(settings)
    report = asyncio.run(run_probe(settings, scenario_ids=cases, keep_bank=keep_bank))
    path = _write_report(
        f"probe-{datetime.now(UTC):%Y%m%dT%H%M%S}", {"provenance": "live_probe", **report.to_dict()}
    )
    typer.echo(json.dumps(report.to_dict(), indent=2, default=str))
    typer.echo(f"Report: {path}")
    if report.problems:
        raise typer.Exit(code=1)


def _default_probe_cases(settings: Settings) -> list[str]:
    """One SEV1 and two non-SEV1 strong-match scenarios, so both reflect budgets are timed."""
    scenarios = [s for s in load_scenarios(settings.data_dir) if s.labels.strong_match]
    sev1 = [s for s in scenarios if s.input.severity == "SEV1"][:1]
    other = [s for s in scenarios if s.input.severity != "SEV1"][:2]
    return [s.input.case_id for s in [*sev1, *other]]


if __name__ == "__main__":
    app()
