"""Workflow evaluation commands.

    pagermind-eval workflow generate                 # (re)build data/evaluation/workflow_cases.jsonl
    pagermind-eval workflow generate --check         # CI: fail if the committed dataset is stale
    pagermind-eval workflow run                      # offline: scripted Groq/Hindsight, real app + Postgres
    pagermind-eval workflow run --mode live --bank-id pagermind-eval-xyz
    pagermind-eval workflow run --category safety --limit 20

`run` needs a Postgres 15+ server: WORKFLOW_EVAL_DATABASE_URL (or TEST_DATABASE_URL) must point at
a database the user can create databases from. A scratch database is created and dropped per run.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from pathlib import Path

import typer
from pydantic import SecretStr

from app.core.config import BACKEND_ROOT, get_settings
from app.core.logging import configure_logging
from evals.workflow.cases import DEFAULT_PATH, WorkflowCase, dump_cases, load_cases
from evals.workflow.database import scratch_database, seed_reference_data
from evals.workflow.generate import generate
from evals.workflow.report import summarise, write_reports
from evals.workflow.runner import CaseResult, WorkflowRunner

app = typer.Typer(help="Evaluate the LangGraph workflow on its own test dataset.", no_args_is_help=True)
REPORT_DIR = BACKEND_ROOT / "evals" / "reports"


@app.command("generate")
def generate_command(
    output: Path = typer.Option(DEFAULT_PATH, help="Where to write the JSONL dataset."),
    check: bool = typer.Option(False, help="Do not write; fail if the file differs from a fresh generation."),
) -> None:
    """Build the dataset deterministically from the service catalog and incident history."""
    cases = generate()
    if check:
        current = output.read_text() if output.exists() else ""
        scratch = output.with_suffix(".check.jsonl")
        dump_cases(cases, scratch)
        fresh = scratch.read_text()
        scratch.unlink()
        if current != fresh:
            typer.echo(f"{output} is stale. Run `pagermind-eval workflow generate`.")
            raise typer.Exit(code=1)
        typer.echo(f"{output} is up to date ({len(cases)} cases).")
        return
    dump_cases(cases, output)
    categories: dict[str, int] = {}
    for case in cases:
        categories[case.category] = categories.get(case.category, 0) + 1
    typer.echo(f"Wrote {len(cases)} cases to {output}")
    for name, count in sorted(categories.items()):
        typer.echo(f"  {name:32} {count}")


async def _evaluate(
    cases: list[WorkflowCase], *, mode: str, admin_url: str, bank_id: str | None, keep_db: bool, verbose: bool
) -> list[CaseResult]:
    base = get_settings()
    with scratch_database(admin_url, keep=keep_db) as url:
        settings = base.model_copy(
            update={
                "app_env": "test",
                "log_level": "WARNING",
                "database_url": SecretStr(url),
                "operator_token": SecretStr(secrets.token_urlsafe(24)),
                "rate_limit_per_minute": 100_000,
                "max_concurrent_agent_runs": 4,
                "langfuse_public_key": "",
                "sentry_dsn": SecretStr(""),
                **({"hindsight_bank_id": bank_id} if bank_id else {}),
            }
        )
        await seed_reference_data(settings)
        runner = (
            WorkflowRunner.offline(settings)
            if mode == "offline"
            else WorkflowRunner.live(settings, bank_id=bank_id or settings.hindsight_bank_id)
        )

        def progress(result: CaseResult) -> None:
            if verbose or not result.passed:
                mark = "PASS" if result.passed else "FAIL"
                typer.echo(f"{mark}  {result.case_id}  {result.category}")

        try:
            return await runner.run(cases, on_case=progress)
        finally:
            await runner.close()


@app.command("run")
def run_command(
    mode: str = typer.Option("offline", help="offline (scripted models) or live (real Groq + Hindsight)."),
    cases_path: Path = typer.Option(DEFAULT_PATH, "--cases", help="JSONL dataset."),
    category: list[str] | None = typer.Option(
        None, help="Restrict to categories or category prefixes (repeatable)."
    ),
    limit: int | None = typer.Option(None, help="Only the first N cases."),
    database_url: str | None = typer.Option(
        None, help="Admin Postgres URL (defaults to WORKFLOW_EVAL_DATABASE_URL)."
    ),
    bank_id: str | None = typer.Option(
        None, help="Live mode: Hindsight bank holding only the historical incidents."
    ),
    fail_under: float = typer.Option(1.0, help="Exit non-zero below this case pass rate."),
    keep_db: bool = typer.Option(False, help="Keep the scratch database for debugging."),
    verbose: bool = typer.Option(False, help="Print every case, not only failures."),
) -> None:
    """Run the workflow evaluation and write JSON + Markdown reports."""
    if mode not in ("offline", "live"):
        raise typer.BadParameter("mode must be 'offline' or 'live'")
    admin_url = database_url or os.getenv("WORKFLOW_EVAL_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        raise typer.BadParameter("Set WORKFLOW_EVAL_DATABASE_URL (or pass --database-url).")
    if mode == "live" and not bank_id:
        raise typer.BadParameter(
            "Live mode needs --bank-id of an evaluation bank (never the production bank)."
        )
    if bank_id and bank_id == get_settings().hindsight_bank_id:
        raise typer.BadParameter("Refusing to evaluate against the production memory bank.")

    configure_logging("WARNING", json_output=False)
    cases = load_cases(cases_path, categories=set(category) if category else None, limit=limit)
    if mode == "live":
        skipped = [c for c in cases if not c.runs_live]
        cases = [c for c in cases if c.runs_live]
        typer.echo(
            f"Live mode: {len(cases)} cases with model-agnostic expectations ({len(skipped)} scripted-only skipped)."
        )
    typer.echo(f"Running {len(cases)} workflow cases ({mode}) · run {uuid.uuid4().hex[:8]}")

    results = asyncio.run(
        _evaluate(cases, mode=mode, admin_url=admin_url, bank_id=bank_id, keep_db=keep_db, verbose=verbose)
    )
    summary = summarise(results)
    json_path, md_path = write_reports(results, summary, mode=mode, directory=REPORT_DIR)

    typer.echo("")
    typer.echo(
        f"{summary['passed']}/{summary['cases']} cases passed ({(summary['pass_rate'] or 0) * 100:.1f}%) · "
        f"leaks {summary['leaks']} · invariant failures {summary['invariant_failures']} · "
        f"crashes {summary['crashed_cases']} · p95 {summary['latency_ms']['p95']} ms"
    )
    for name, row in summary["by_category"].items():
        typer.echo(f"  {name:32} {row['passed']:>3}/{row['cases']:<3} {(row['pass_rate'] or 0) * 100:5.1f}%")
    typer.echo(f"Reports: {md_path}  ·  {json_path}")
    if (summary["pass_rate"] or 0) < fail_under:
        raise typer.Exit(code=1)
