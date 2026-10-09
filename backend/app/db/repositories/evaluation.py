"""Evaluation cases, runs and results (written by CI only)."""

from __future__ import annotations

from typing import Any

from app.db.json import jsonb
from app.db.pool import Connection


async def upsert_case(
    conn: Connection,
    *,
    case_id: str,
    category: str,
    service_id: str | None,
    prompt: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    await conn.execute(
        """
        insert into evaluation_cases (case_id, category, service_id, prompt, expected)
        values (%s, %s, %s, %s, %s)
        on conflict (case_id) do update set
            category = excluded.category, prompt = excluded.prompt, expected = excluded.expected
        """,
        (case_id, category, service_id, jsonb(prompt), jsonb(expected)),
    )


async def start_run(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into evaluation_runs (evaluation_run_id, commit_sha, bank_id, chat_model, judge_model, repetitions)
        values (%(evaluation_run_id)s, %(commit_sha)s, %(bank_id)s, %(chat_model)s, %(judge_model)s,
                %(repetitions)s)
        """,
        row,
    )


async def insert_result(conn: Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        """
        insert into evaluation_results (
            evaluation_result_id, evaluation_run_id, case_id, mode, repetition, retrieved_incident_ids,
            predicted_strong_match, abstained, schema_valid, scores, response, latency_ms, input_tokens,
            output_tokens, cost_usd, error
        ) values (
            %(evaluation_result_id)s, %(evaluation_run_id)s, %(case_id)s, %(mode)s, %(repetition)s,
            %(retrieved_incident_ids)s, %(predicted_strong_match)s, %(abstained)s, %(schema_valid)s, %(scores)s,
            %(response)s, %(latency_ms)s, %(input_tokens)s, %(output_tokens)s, %(cost_usd)s, %(error)s
        )
        on conflict (evaluation_run_id, case_id, mode, repetition) do nothing
        """,
        {**row, "scores": jsonb(row["scores"]), "response": jsonb(row.get("response"))},
    )


async def finish_run(
    conn: Connection, evaluation_run_id: str, *, status: str, summary: dict[str, Any]
) -> None:
    await conn.execute(
        """
        update evaluation_runs set status = %s, summary = %s, finished_at = now()
        where evaluation_run_id = %s
        """,
        (status, jsonb(summary), evaluation_run_id),
    )
