"""Health snapshots and guardrail decisions."""

from __future__ import annotations

from typing import Any

from app.db.json import jsonb
from app.db.pool import Connection


async def insert_health_snapshot(
    conn: Connection, *, overall_status: str, dependencies: dict[str, Any], pending_memory_jobs: int | None
) -> None:
    await conn.execute(
        """
        insert into health_snapshots (overall_status, dependencies, pending_memory_jobs)
        values (%s, %s, %s)
        """,
        (overall_status, jsonb(dependencies), pending_memory_jobs),
    )


async def insert_guardrail_results(
    conn: Connection,
    request_id: str | None,
    results: list[dict[str, Any]],
    evaluation_run_id: str | None = None,
) -> None:
    if not results:
        return
    async with conn.cursor() as cursor:
        await cursor.executemany(
            """
            insert into guardrail_results (evaluation_run_id, request_id, phase, check_name, decision,
                                           reason_code, details)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    evaluation_run_id,
                    request_id,
                    result["phase"],
                    result["check_name"],
                    result["decision"],
                    result.get("reason_code"),
                    jsonb(result.get("details") or {}),
                )
                for result in results
            ],
        )


async def latest_health_snapshot(conn: Connection) -> dict[str, Any] | None:
    """Latest snapshot measured by the worker (fixtures imported from the dataset are excluded)."""
    cursor = await conn.execute(
        """
        select overall_status, dependencies, checked_at from health_snapshots
        where provenance = 'live' order by checked_at desc limit 1
        """
    )
    return await cursor.fetchone()


async def insert_fixture_snapshot(
    conn: Connection, *, snapshot_id: str, overall_status: str, dependencies: dict[str, Any], checked_at: Any
) -> None:
    """Seeded dashboard examples, explicitly labelled so they are never read as live health."""
    await conn.execute(
        """
        insert into health_snapshots (health_snapshot_id, overall_status, dependencies, provenance, checked_at)
        values (%s, %s, %s, 'synthetic_fixture', %s)
        on conflict (health_snapshot_id) do nothing
        """,
        (snapshot_id, overall_status, jsonb(dependencies), checked_at),
    )


async def prune_expired(
    conn: Connection, *, webhook_days: int, health_snapshot_days: int, guardrail_result_days: int
) -> dict[str, int]:
    """Delete operational telemetry past its retention window. Returns rows removed per table."""
    removed: dict[str, int] = {}
    statements = {
        # Claims reference deliveries, so they go first; both are past any redelivery window.
        "memory_webhook_claims": (
            "delete from memory_webhook_claims where claimed_at < now() - make_interval(days => %s)",
            webhook_days,
        ),
        "memory_webhook_events": (
            """
            delete from memory_webhook_events e
            where e.received_at < now() - make_interval(days => %s)
              and not exists (select 1 from memory_webhook_claims c where c.webhook_event_id = e.webhook_event_id)
            """,
            webhook_days,
        ),
        "health_snapshots": (
            "delete from health_snapshots where provenance = 'live' and checked_at < now() - make_interval(days => %s)",
            health_snapshot_days,
        ),
        "guardrail_results": (
            "delete from guardrail_results where evaluation_run_id is null "
            "and created_at < now() - make_interval(days => %s)",
            guardrail_result_days,
        ),
    }
    for table, (statement, days) in statements.items():
        cursor = await conn.execute(statement, (days,))
        removed[table] = cursor.rowcount
    return removed
