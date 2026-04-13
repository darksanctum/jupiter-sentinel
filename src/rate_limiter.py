"""
Smart Jupiter API rate limiter with priority-aware scheduling.

Jupiter does not expose a bulk quote endpoint. "Batching" here means
coalescing identical pending quote requests into a single outbound call and
sharing the result with every waiter.
"""

from __future__ import annotations
import logging

import heapq
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Deque, Hashable

from .jupiter_limits import FREE_PLAN_REQUESTS_PER_MINUTE, FREE_PLAN_WINDOW_SECONDS

QUOTE_ENDPOINT = "quote"
SWAP_ENDPOINT = "swap"
PROGRAM_ID_TO_LABEL_ENDPOINT = "program-id-to-label"


class JupiterRequestPriority(IntEnum):
    """Lower values win when the queue needs to shed load."""

    EXECUTION = 0
    POSITION_MONITORING = 10
    ROUTE_INTELLIGENCE = 20
    SCANNING = 30


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    """Canonical representation of a Jupiter quote request."""

    input_mint: str
    output_mint: str
    amount: int
    slippage_bps: int = 50
    only_direct_routes: bool | None = None
    as_legacy_transaction: bool | None = None

    @property
    def batch_key(self) -> tuple[Any, ...]:
        """Function docstring."""
        return (
            self.input_mint,
            self.output_mint,
            int(self.amount),
            int(self.slippage_bps),
            self.only_direct_routes,
            self.as_legacy_transaction,
        )


class TokenBucket:
    """Simple token bucket for a sliding-window-style request budget."""

    def __init__(
        self,
        capacity: int = FREE_PLAN_REQUESTS_PER_MINUTE,
        window_seconds: float = float(FREE_PLAN_WINDOW_SECONDS),
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Function docstring."""
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self.capacity = float(capacity)
        self.window_seconds = float(window_seconds)
        self.refill_rate = self.capacity / self.window_seconds
        self._clock = clock
        self._tokens = self.capacity
        self._updated_at = self._clock()

    def _refill(self, now: float) -> None:
        """Function docstring."""
        elapsed = max(0.0, now - self._updated_at)
        if elapsed:
            self._tokens = min(
                self.capacity, self._tokens + (elapsed * self.refill_rate)
            )
            self._updated_at = now

    def available_tokens(self, *, now: float | None = None) -> float:
        """Function docstring."""
        current_time = self._clock() if now is None else now
        self._refill(current_time)
        return self._tokens

    def consume(self, tokens: float = 1.0, *, now: float | None = None) -> bool:
        """Function docstring."""
        if tokens <= 0:
            raise ValueError("tokens must be positive")

        current_time = self._clock() if now is None else now
        self._refill(current_time)
        if self._tokens + 1e-9 < tokens:
            return False

        self._tokens -= tokens
        return True

    def time_until_available(
        self, tokens: float = 1.0, *, now: float | None = None
    ) -> float:
        """Function docstring."""
        if tokens <= 0:
            raise ValueError("tokens must be positive")

        current_time = self._clock() if now is None else now
        self._refill(current_time)
        if self._tokens + 1e-9 >= tokens:
            return 0.0
        return (tokens - self._tokens) / self.refill_rate


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    endpoint: str
    queued_requests: int
    started_calls: int
    completed_calls: int
    failed_calls: int
    calls_in_window: int
    last_called_at: float | None


@dataclass(slots=True)
class _EndpointStats:
    queued_requests: int = 0
    started_calls: int = 0
    completed_calls: int = 0
    failed_calls: int = 0
    last_called_at: float | None = None
    recent_call_timestamps: Deque[float] = field(default_factory=deque)


@dataclass(slots=True)
class _QueuedRequest:
    endpoint: str
    operation: Callable[[], Any]
    priority: int
    queued_at: float
    ready_at: float
    sequence: int
    waiters: list[Future[Any]] = field(default_factory=list)
    batch_key: Hashable | None = None
    submitted_requests: int = 1
    version: int = 0
    dispatched: bool = False


class JupiterRateLimiter:
    """
    Queue-backed Jupiter rate limiter using a token bucket and request priority.

    The limiter enforces a shared outbound rate budget while still exposing
    per-endpoint stats so callers can see whether scans are starving more
    important monitoring or execution paths.
    """

    def __init__(
        self,
        *,
        max_requests: int = FREE_PLAN_REQUESTS_PER_MINUTE,
        window_seconds: float = float(FREE_PLAN_WINDOW_SECONDS),
        quote_batch_window_seconds: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Function docstring."""
        if quote_batch_window_seconds < 0:
            raise ValueError("quote_batch_window_seconds cannot be negative")

        self._clock = clock
        self._sleep = sleep
        self._quote_batch_window_seconds = float(quote_batch_window_seconds)
        self._bucket = TokenBucket(max_requests, window_seconds, clock=clock)
        self._lock = threading.RLock()
        self._queue: list[tuple[int, int, int, _QueuedRequest]] = []
        self._sequence = 0
        self._pending_batches: dict[tuple[str, Hashable], _QueuedRequest] = {}
        self._stats: dict[str, _EndpointStats] = defaultdict(_EndpointStats)
        self._window_seconds = float(window_seconds)

    def submit(
        self,
        endpoint: str,
        operation: Callable[[], Any],
        *,
        priority: JupiterRequestPriority | int = JupiterRequestPriority.SCANNING,
        batch_key: Hashable | None = None,
    ) -> Future[Any]:
        """Queue a request and return a future resolved once it executes."""
        normalized_endpoint = endpoint.strip()
        if not normalized_endpoint:
            raise ValueError("endpoint cannot be empty")

        future: Future[Any] = Future()

        with self._lock:
            stats = self._stats[normalized_endpoint]
            stats.queued_requests += 1

            batch_lookup = None
            if batch_key is not None:
                batch_lookup = (normalized_endpoint, batch_key)
                existing = self._pending_batches.get(batch_lookup)
                if existing and not existing.dispatched:
                    existing.waiters.append(future)
                    existing.submitted_requests += 1
                    new_priority = min(existing.priority, int(priority))
                    if new_priority != existing.priority:
                        existing.priority = new_priority
                        existing.version += 1
                        heapq.heappush(
                            self._queue,
                            (
                                existing.priority,
                                existing.sequence,
                                existing.version,
                                existing,
                            ),
                        )
                    return future

            now = self._clock()
            request = _QueuedRequest(
                endpoint=normalized_endpoint,
                operation=operation,
                priority=int(priority),
                queued_at=now,
                ready_at=now
                + (self._quote_batch_window_seconds if batch_key is not None else 0.0),
                sequence=self._sequence,
                waiters=[future],
                batch_key=batch_key,
            )
            self._sequence += 1

            heapq.heappush(
                self._queue,
                (request.priority, request.sequence, request.version, request),
            )
            if batch_lookup is not None:
                self._pending_batches[batch_lookup] = request

        return future

    def submit_quote(
        self,
        quote_request: QuoteRequest,
        operation: Callable[[], Any],
        *,
        priority: JupiterRequestPriority | int = JupiterRequestPriority.SCANNING,
    ) -> Future[Any]:
        """Queue a quote request with batching keyed by quote parameters."""
        return self.submit(
            QUOTE_ENDPOINT,
            operation,
            priority=priority,
            batch_key=quote_request.batch_key,
        )

    def call(
        self,
        endpoint: str,
        operation: Callable[[], Any],
        *,
        priority: JupiterRequestPriority | int = JupiterRequestPriority.SCANNING,
        batch_key: Hashable | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Queue a request and block until its future is resolved."""
        future = self.submit(
            endpoint, operation, priority=priority, batch_key=batch_key
        )
        deadline = None if timeout is None else self._clock() + timeout

        while True:
            if future.done():
                return future.result()

            if self.run_next():
                continue

            sleep_for = max(self.time_until_next_dispatch(), 0.001)
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out waiting for {endpoint} request to run"
                    )
                sleep_for = min(sleep_for, remaining)

            self._sleep(sleep_for)

    def call_quote(
        self,
        quote_request: QuoteRequest,
        operation: Callable[[], Any],
        *,
        priority: JupiterRequestPriority | int = JupiterRequestPriority.SCANNING,
        timeout: float | None = None,
    ) -> Any:
        """Blocking convenience wrapper for quote requests."""
        return self.call(
            QUOTE_ENDPOINT,
            operation,
            priority=priority,
            batch_key=quote_request.batch_key,
            timeout=timeout,
        )

    def run_next(self) -> bool:
        """
        Execute the highest-priority request that is both batch-ready and token-ready.
        """
        with self._lock:
            request = self._peek_next_valid_request()
            if request is None:
                return False

            now = self._clock()
            if request.ready_at > now:
                return False
            if not self._bucket.consume(now=now):
                return False

            request.dispatched = True
            if request.batch_key is not None:
                self._pending_batches.pop((request.endpoint, request.batch_key), None)

            stats = self._stats[request.endpoint]
            stats.queued_requests = max(
                0, stats.queued_requests - request.submitted_requests
            )
            stats.started_calls += 1
            stats.last_called_at = now
            stats.recent_call_timestamps.append(now)

        try:
            result = request.operation()
        except Exception as exc:
            with self._lock:
                self._stats[request.endpoint].failed_calls += 1
            for waiter in request.waiters:
                waiter.set_exception(exc)
        else:
            with self._lock:
                self._stats[request.endpoint].completed_calls += 1
            for waiter in request.waiters:
                waiter.set_result(result)

        return True

    def run_until_idle(
        self, *, block: bool = False, max_calls: int | None = None
    ) -> int:
        """
        Drain the queue until nothing else can run immediately.

        When ``block`` is true, the limiter sleeps until tokens or batch windows
        free up and keeps draining until the queue is empty.
        """
        completed = 0
        while True:
            if max_calls is not None and completed >= max_calls:
                return completed

            if self.run_next():
                completed += 1
                continue

            pending = self.pending_requests()
            if pending == 0 or not block:
                return completed

            self._sleep(max(self.time_until_next_dispatch(), 0.001))

    def time_until_next_dispatch(self) -> float:
        """Return the delay before the queue head can execute."""
        with self._lock:
            request = self._peek_next_valid_request()
            if request is None:
                return 0.0

            now = self._clock()
            batch_delay = max(0.0, request.ready_at - now)
            token_delay = self._bucket.time_until_available(now=now)
            return max(batch_delay, token_delay)

    def pending_requests(self, endpoint: str | None = None) -> int:
        """Return pending logical requests globally or for one endpoint."""
        with self._lock:
            if endpoint is None:
                return sum(stats.queued_requests for stats in self._stats.values())
            return self._stats[endpoint].queued_requests

    def endpoint_snapshot(self, endpoint: str) -> EndpointSnapshot:
        """Return current counters for one endpoint."""
        with self._lock:
            stats = self._stats[endpoint]
            now = self._clock()
            self._prune_call_window(stats, now)
            return EndpointSnapshot(
                endpoint=endpoint,
                queued_requests=stats.queued_requests,
                started_calls=stats.started_calls,
                completed_calls=stats.completed_calls,
                failed_calls=stats.failed_calls,
                calls_in_window=len(stats.recent_call_timestamps),
                last_called_at=stats.last_called_at,
            )

    def snapshot(self) -> dict[str, Any]:
        """Return limiter state suitable for logging or debugging."""
        with self._lock:
            now = self._clock()
            endpoints = {}
            for endpoint, stats in self._stats.items():
                self._prune_call_window(stats, now)
                endpoints[endpoint] = EndpointSnapshot(
                    endpoint=endpoint,
                    queued_requests=stats.queued_requests,
                    started_calls=stats.started_calls,
                    completed_calls=stats.completed_calls,
                    failed_calls=stats.failed_calls,
                    calls_in_window=len(stats.recent_call_timestamps),
                    last_called_at=stats.last_called_at,
                )

            return {
                "available_tokens": self._bucket.available_tokens(now=now),
                "pending_requests": sum(
                    stats.queued_requests for stats in self._stats.values()
                ),
                "endpoints": endpoints,
            }

    def _peek_next_valid_request(self) -> _QueuedRequest | None:
        """Function docstring."""
        while self._queue:
            priority, sequence, version, request = self._queue[0]
            if request.dispatched or request.version != version:
                heapq.heappop(self._queue)
                continue
            if request.priority != priority or request.sequence != sequence:
                heapq.heappop(self._queue)
                continue
            return request
        return None

    def _prune_call_window(self, stats: _EndpointStats, now: float) -> None:
        """Function docstring."""
        cutoff = now - self._window_seconds
        while stats.recent_call_timestamps and stats.recent_call_timestamps[0] < cutoff:
            stats.recent_call_timestamps.popleft()
