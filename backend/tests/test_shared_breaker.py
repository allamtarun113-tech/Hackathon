from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.errors import DependencyUnavailable
from app.core.resilience import CircuitBreaker, call_with_resilience


@dataclass
class State:
    consecutive_failures: int
    open_seconds_left: float


class MemoryStore:
    """In-memory stand-in for the Postgres store: one row per breaker name."""

    def __init__(self) -> None:
        self.failures: dict[str, int] = {}
        self.open: dict[str, float] = {}
        self.down = False

    async def read(self, name: str) -> State:
        if self.down:
            raise ConnectionError("store down")
        return State(self.failures.get(name, 0), self.open.get(name, 0.0))

    async def record_failure(self, name: str, *, threshold: int, open_for: float) -> State:
        if self.down:
            raise ConnectionError("store down")
        self.failures[name] = self.failures.get(name, 0) + 1
        if self.failures[name] >= threshold:
            self.open[name] = open_for
        return State(self.failures[name], self.open.get(name, 0.0))

    async def record_success(self, name: str) -> None:
        self.failures[name], self.open[name] = 0, 0.0


async def _fail() -> None:
    raise ConnectionError("dependency down")


async def _ok() -> str:
    return "ok"


async def _call(breaker: CircuitBreaker, operation=_fail) -> object:  # type: ignore[no-untyped-def]
    return await call_with_resilience(operation, breaker=breaker, attempt_timeout=1, attempts=1)


async def test_failures_on_one_instance_open_the_breaker_for_all() -> None:
    store = MemoryStore()
    api_a = CircuitBreaker("hindsight", failure_threshold=4, store=store, sync_every=0)
    api_b = CircuitBreaker("hindsight", failure_threshold=4, store=store, sync_every=0)
    for breaker in (api_a, api_b, api_a, api_b):  # two failures each: neither alone reaches the threshold
        with pytest.raises(ConnectionError):
            await _call(breaker)
    with pytest.raises(DependencyUnavailable):
        await _call(api_b)  # the fourth deployment-wide failure opened it
    api_c = CircuitBreaker("hindsight", failure_threshold=4, store=store, sync_every=0)
    with pytest.raises(DependencyUnavailable):
        await _call(api_c, _ok)  # a fresh instance learns it is open without failing itself


async def test_a_success_anywhere_resets_the_shared_count() -> None:
    store = MemoryStore()
    api_a = CircuitBreaker("groq", failure_threshold=3, store=store, sync_every=0)
    api_b = CircuitBreaker("groq", failure_threshold=3, store=store, sync_every=0)
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await _call(api_a)
    assert await _call(api_b, _ok) == "ok"
    assert store.failures["groq"] == 0


async def test_a_store_outage_never_blocks_calls() -> None:
    store = MemoryStore()
    store.down = True
    breaker = CircuitBreaker("hindsight", failure_threshold=2, store=store, sync_every=0)
    assert await _call(breaker, _ok) == "ok"
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await _call(breaker)
    with pytest.raises(DependencyUnavailable):
        await _call(breaker)  # local protection still works
