"""Fixed-window counters shared by every API instance."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.db.pool import Connection


async def hit(conn: Connection, *, key: str, window_start: datetime) -> int:
    """Count one request in the window and return the window's total."""
    cursor = await conn.execute(
        """
        insert into rate_limit_buckets (bucket_key, window_start, hits) values (%s, %s, 1)
        on conflict (bucket_key, window_start) do update set hits = rate_limit_buckets.hits + 1
        returning hits
        """,
        (key, window_start),
    )
    row = await cursor.fetchone()
    return int(row["hits"]) if row else 1


async def prune(conn: Connection, *, older_than: timedelta) -> int:
    cursor = await conn.execute(
        "delete from rate_limit_buckets where window_start < now() - %s", (older_than,)
    )
    return cursor.rowcount
