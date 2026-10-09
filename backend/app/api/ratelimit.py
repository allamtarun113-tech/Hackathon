"""Rate limiting.

`PostgresRateLimiter` keeps fixed-window counters in Postgres so the limit holds across
every API instance. If the database is unreachable it falls back to a per-process
sliding window (`MemoryRateLimiter`) rather than rejecting all traffic.

Client identity comes from `client_ip`, which only trusts X-Forwarded-For entries
appended by a known number of reverse proxies; anything a client writes into the
header itself is ignored.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.db.pool import Database
from app.db.repositories import rate_limits

log = get_logger(__name__)


def client_ip(forwarded_for: str | None, peer: str | None, *, trusted_hops: int) -> str:
    """Pick the address added by the outermost trusted proxy.

    With N trusted proxies, the last N entries of X-Forwarded-For were written by them; the
    entry they appended for the connecting client is therefore the Nth from the right.
    """
    if trusted_hops <= 0 or not forwarded_for:
        return peer or "unknown"
    hops: Sequence[str] = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(hops) < trusted_hops:
        return peer or "unknown"
    return hops[-trusted_hops]


class MemoryRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, seconds_until_retry)."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False, max(1, int(self._window - (now - hits[0])))
        hits.append(now)
        if len(self._hits) > 10_000:
            self._evict(now)
        return True, 0

    def _evict(self, now: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or now - hits[-1] >= self._window]
        for key in stale:
            del self._hits[key]


class PostgresRateLimiter:
    def __init__(self, db: Database, *, limit: int, window_seconds: int) -> None:
        self._db = db
        self._limit = limit
        self._window = window_seconds
        self._fallback = MemoryRateLimiter(limit=limit, window_seconds=window_seconds)

    async def allow(self, key: str) -> tuple[bool, int]:
        now = datetime.now(UTC)
        window_start = datetime.fromtimestamp(int(now.timestamp()) // self._window * self._window, tz=UTC)
        try:
            async with self._db.transaction() as conn:
                hits = await rate_limits.hit(conn, key=key, window_start=window_start)
        except Exception as error:
            log.warning("rate_limit_store_unavailable", error_type=type(error).__name__)
            return self._fallback.allow(key)

        if hits > self._limit:
            retry_after = self._window - int(now.timestamp() - window_start.timestamp())
            return False, max(1, retry_after)
        return True, 0
