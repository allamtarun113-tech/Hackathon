"""Timeouts, retries and a small circuit breaker for external dependencies."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.core.errors import DependencyUnavailable
from app.core.logging import get_logger

log = get_logger(__name__)


STORE_TIMEOUT_SECONDS = 1.0


class SharedState(Protocol):
    @property
    def consecutive_failures(self) -> int: ...
    @property
    def open_seconds_left(self) -> float: ...


class BreakerStore(Protocol):
    """Deployment-wide breaker state (Postgres in production)."""

    async def read(self, name: str) -> SharedState: ...
    async def record_failure(self, name: str, *, threshold: int, open_for: float) -> SharedState: ...
    async def record_success(self, name: str) -> None: ...


@dataclass
class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures and stays open for `reset_after` seconds.

    Without a store the state is per process. With one, failures and recoveries are shared by every
    API instance and worker: the deployment stops calling a dead dependency together, and the shared
    state is re-read at most every `sync_every` seconds. The store is advisory: if it is slow or down,
    the breaker keeps working on local state and never blocks a call because of it.
    """

    name: str
    failure_threshold: int = 5
    reset_after: float = 30.0
    store: BreakerStore | None = None
    sync_every: float = 2.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _synced_at: float = field(default=float("-inf"), init=False)
    _remote_failures: int = field(default=0, init=False)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after:
            # Half-open: allow the next call through as a probe.
            self._opened_at = None
            self._failures = self.failure_threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            log.warning("circuit_opened", dependency=self.name)

    def _open_for(self, seconds: float) -> None:
        if self._opened_at is None:
            log.warning("circuit_opened", dependency=self.name, source="shared")
        self._failures = self.failure_threshold
        self._opened_at = time.monotonic() - self.reset_after + seconds

    async def allow(self) -> bool:
        """Whether a call may proceed, consulting the shared state when it is due for a refresh."""
        if self.is_open:
            return False
        if self.store is None or time.monotonic() - self._synced_at < self.sync_every:
            return True
        self._synced_at = time.monotonic()
        try:
            state = await asyncio.wait_for(self.store.read(self.name), STORE_TIMEOUT_SECONDS)
        except Exception as error:
            log.debug("circuit_store_unavailable", dependency=self.name, error_type=type(error).__name__)
            return True
        self._remote_failures = state.consecutive_failures
        if state.open_seconds_left > 0:
            self._open_for(state.open_seconds_left)
            return False
        return True

    async def failed(self) -> None:
        self.record_failure()
        if self.store is None:
            return
        try:
            state = await asyncio.wait_for(
                self.store.record_failure(
                    self.name, threshold=self.failure_threshold, open_for=self.reset_after
                ),
                STORE_TIMEOUT_SECONDS,
            )
        except Exception as error:
            log.debug("circuit_store_unavailable", dependency=self.name, error_type=type(error).__name__)
            return
        self._remote_failures = state.consecutive_failures
        if state.open_seconds_left > 0 and not self.is_open:
            self._open_for(state.open_seconds_left)

    async def succeeded(self) -> None:
        shared_failures = self._failures > 0 or self._remote_failures > 0
        self.record_success()
        if self.store is None or not shared_failures:
            return
        try:
            await asyncio.wait_for(self.store.record_success(self.name), STORE_TIMEOUT_SECONDS)
            self._remote_failures = 0
        except Exception as error:
            log.debug("circuit_store_unavailable", dependency=self.name, error_type=type(error).__name__)


async def call_with_resilience[T](
    operation: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker,
    attempt_timeout: float,
    attempts: int = 3,
    retry_on: Callable[[BaseException], bool] = lambda _: True,
) -> T:
    """Run `operation` with a per-attempt timeout, jittered exponential retries and a circuit breaker."""
    if not await breaker.allow():
        raise DependencyUnavailable(f"{breaker.name} is temporarily unavailable (circuit open).")

    def _should_retry(error: BaseException) -> bool:
        return isinstance(error, TimeoutError) or retry_on(error)

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential_jitter(initial=0.5, max=4),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                result = await asyncio.wait_for(operation(), timeout=attempt_timeout)
    except Exception:
        await breaker.failed()
        raise
    await breaker.succeeded()
    return result
