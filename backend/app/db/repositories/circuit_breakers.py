"""Circuit breaker state shared across processes (see app.core.resilience.CircuitBreaker)."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.pool import Database


@dataclass(frozen=True)
class SharedBreakerState:
    consecutive_failures: int
    open_seconds_left: float


class PostgresBreakerStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def read(self, name: str) -> SharedBreakerState:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                select consecutive_failures,
                       greatest(0, extract(epoch from (opened_until - now())))::float as open_seconds_left
                from circuit_breakers where name = %s
                """,
                (name,),
            )
            row = await cursor.fetchone()
        if row is None:
            return SharedBreakerState(0, 0.0)
        return SharedBreakerState(int(row["consecutive_failures"]), float(row["open_seconds_left"] or 0.0))

    async def record_failure(self, name: str, *, threshold: int, open_for: float) -> SharedBreakerState:
        """Count one failure deployment-wide; the call that reaches the threshold opens the breaker."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                insert into circuit_breakers (name, consecutive_failures) values (%(name)s, 1)
                on conflict (name) do update
                    set consecutive_failures = circuit_breakers.consecutive_failures + 1, updated_at = now()
                returning consecutive_failures
                """,
                {"name": name},
            )
            row = await cursor.fetchone()
            failures = int(row["consecutive_failures"]) if row else 1
            if failures >= threshold:
                await conn.execute(
                    """
                    update circuit_breakers
                    set opened_until = now() + make_interval(secs => %s)
                    where name = %s and (opened_until is null or opened_until < now())
                    """,
                    (open_for, name),
                )
        return SharedBreakerState(failures, open_for if failures >= threshold else 0.0)

    async def record_success(self, name: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                update circuit_breakers
                set consecutive_failures = 0, opened_until = null, updated_at = now()
                where name = %s and (consecutive_failures > 0 or opened_until is not null)
                """,
                (name,),
            )
