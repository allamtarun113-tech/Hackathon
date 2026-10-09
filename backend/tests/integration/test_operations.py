"""Backpressure, retention and indexed lookups."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.db.pool import Database
from app.db.repositories import memory, observability, rate_limits
from tests.integration.conftest import Externals, build_client
from tests.integration.test_outbox_and_worker import _document, _enqueue


async def test_chat_returns_503_when_the_instance_is_at_capacity(
    settings: Settings, externals: Externals
) -> None:
    async with build_client(
        settings.model_copy(update={"max_concurrent_agent_runs": 1}), externals
    ) as client:
        agent = client._transport.app.state.container.agent  # type: ignore[attr-defined]
        async with agent._slots:  # occupy the only slot
            response = await client.post(
                "/chat", json={"engineer_id": "priya", "message": "payments-api down"}
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dependency_unavailable"


async def test_job_lookup_falls_back_to_the_stable_operation_id(db: Database) -> None:
    job = await _enqueue(db, _document())
    async with db.transaction() as conn:
        stored = await memory.get_job(conn, job["job_id"])
        assert stored is not None
        by_uuid = await memory.job_by_operation(conn, str(stored["operation_id"]))
        assert by_uuid is not None and by_uuid["job_id"] == job["job_id"]
        assert await memory.job_by_operation(conn, "not-a-uuid") is None


async def test_retention_prunes_only_expired_telemetry(db: Database) -> None:
    async with db.transaction() as conn:
        await observability.insert_health_snapshot(
            conn, overall_status="healthy", dependencies={}, pending_memory_jobs=0
        )
        await conn.execute(
            "insert into health_snapshots (overall_status, dependencies, checked_at) "
            "values ('healthy', '{}', now() - interval '90 days')"
        )
        await conn.execute(
            "insert into health_snapshots (overall_status, dependencies, provenance, checked_at) "
            "values ('healthy', '{}', 'synthetic_fixture', now() - interval '400 days')"
        )
        removed = await observability.prune_expired(
            conn, webhook_days=14, health_snapshot_days=30, guardrail_result_days=90
        )
        cursor = await conn.execute("select provenance from health_snapshots order by checked_at")
        remaining = [row["provenance"] for row in await cursor.fetchall()]
    assert removed["health_snapshots"] == 1
    assert remaining == ["synthetic_fixture", "live"]  # fixtures are never pruned


async def test_concurrent_rate_limit_hits_are_counted_exactly(
    app_client: httpx.AsyncClient, db: Database
) -> None:
    window = datetime(2026, 1, 1, tzinfo=UTC)

    async def hit() -> int:
        async with db.transaction() as conn:
            return await rate_limits.hit(conn, key="ip:test", window_start=window)

    counts = await asyncio.gather(*(hit() for _ in range(10)))
    assert sorted(counts) == list(range(1, 11))


async def test_postgres_breaker_store_is_shared_and_resets(db: Database) -> None:
    from app.db.repositories.circuit_breakers import PostgresBreakerStore

    first, second = PostgresBreakerStore(db), PostgresBreakerStore(db)  # two processes, one database
    for _ in range(2):
        await first.record_failure("hindsight", threshold=3, open_for=30)
    opened = await second.record_failure("hindsight", threshold=3, open_for=30)
    assert opened.consecutive_failures == 3
    state = await first.read("hindsight")
    assert 25 < state.open_seconds_left <= 30
    await second.record_success("hindsight")
    assert await first.read("hindsight") == (await second.read("hindsight"))
    assert (await first.read("hindsight")).open_seconds_left == 0
