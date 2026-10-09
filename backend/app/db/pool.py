"""Async Postgres connection pool (Supabase Postgres)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings

Connection = AsyncConnection[DictRow]
Role = Literal["api", "worker", "cli"]


class Database:
    """One pool per process. The role sets the pool size and tags connections in pg_stat_activity."""

    def __init__(self, settings: Settings, *, role: Role = "cli") -> None:
        max_size = (
            settings.worker_database_pool_max_size if role != "api" else settings.database_pool_max_size
        )
        server_options = (
            f"-c statement_timeout={settings.database_statement_timeout_ms} "
            f"-c idle_in_transaction_session_timeout={settings.database_idle_in_transaction_timeout_ms}"
        )
        self._pool: AsyncConnectionPool[Connection] = AsyncConnectionPool(
            conninfo=settings.database_url.get_secret_value(),
            min_size=min(settings.database_pool_min_size, max_size),
            max_size=max_size,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                # Transaction-mode poolers (Supabase/PgBouncer) cannot use server-side prepared statements.
                "prepare_threshold": None,
                "application_name": f"pagermind-{role}",
                "options": server_options,
            },
            open=False,
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=15)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """Yield a connection inside a transaction; commits on success, rolls back on error."""
        async with self._pool.connection() as conn, conn.transaction():
            yield conn

    async def ping(self) -> None:
        async with self.transaction() as conn:
            await conn.execute("select 1")
