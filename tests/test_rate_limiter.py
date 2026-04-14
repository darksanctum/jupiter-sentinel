from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.rate_limiter import (
    QUOTE_ENDPOINT,
    SWAP_ENDPOINT,
    EndpointSnapshot,
    JupiterRateLimiter,
    JupiterRequestPriority,
    QuoteRequest,
)


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_token_bucket_enforces_five_requests_per_ten_seconds() -> None:
    clock = FakeClock()
    limiter = JupiterRateLimiter(
        max_requests=5,
        window_seconds=10,
        clock=clock.now,
        sleep=clock.sleep,
        quote_batch_window_seconds=0.0,
    )
    executed: list[int] = []

    futures = [
        limiter.submit(
            QUOTE_ENDPOINT,
            lambda value=value: executed.append(value) or value,
            priority=JupiterRequestPriority.SCANNING,
        )
        for value in range(6)
    ]

    assert limiter.run_until_idle() == 5
    assert executed == [0, 1, 2, 3, 4]
    assert not futures[5].done()
    assert limiter.time_until_next_dispatch() == pytest.approx(2.0)

    clock.advance(2.0)
    assert limiter.run_next() is True
    assert executed == [0, 1, 2, 3, 4, 5]
    assert futures[5].result() == 5


def test_position_monitoring_runs_before_scanning() -> None:
    clock = FakeClock()
    limiter = JupiterRateLimiter(
        clock=clock.now,
        sleep=clock.sleep,
        quote_batch_window_seconds=0.0,
    )
    executed: list[str] = []

    scanning_future = limiter.submit(
        QUOTE_ENDPOINT,
        lambda: executed.append("scan") or "scan",
        priority=JupiterRequestPriority.SCANNING,
    )
    monitoring_future = limiter.submit(
        QUOTE_ENDPOINT,
        lambda: executed.append("monitor") or "monitor",
        priority=JupiterRequestPriority.POSITION_MONITORING,
    )

    assert limiter.run_next() is True
    assert executed == ["monitor"]
    assert monitoring_future.result() == "monitor"
    assert not scanning_future.done()

    assert limiter.run_next() is True
    assert executed == ["monitor", "scan"]
    assert scanning_future.result() == "scan"


def test_identical_quote_requests_are_batched_into_one_call() -> None:
    clock = FakeClock()
    limiter = JupiterRateLimiter(
        clock=clock.now,
        sleep=clock.sleep,
        quote_batch_window_seconds=0.05,
    )
    request = QuoteRequest("mint-in", "mint-out", 1_000_000, slippage_bps=50)
    outbound_calls = 0

    def fetch_quote() -> dict[str, str]:
        nonlocal outbound_calls
        outbound_calls += 1
        return {"outAmount": "12345"}

    first = limiter.submit_quote(
        request,
        fetch_quote,
        priority=JupiterRequestPriority.SCANNING,
    )
    second = limiter.submit_quote(
        request,
        fetch_quote,
        priority=JupiterRequestPriority.POSITION_MONITORING,
    )

    assert limiter.run_next() is False
    clock.advance(0.05)
    assert limiter.run_next() is True
    assert outbound_calls == 1
    assert first.result() == {"outAmount": "12345"}
    assert second.result() == {"outAmount": "12345"}

    snapshot = limiter.endpoint_snapshot(QUOTE_ENDPOINT)
    assert snapshot == EndpointSnapshot(
        endpoint=QUOTE_ENDPOINT,
        queued_requests=0,
        started_calls=1,
        completed_calls=1,
        failed_calls=0,
        calls_in_window=1,
        last_called_at=pytest.approx(0.05),
    )


def test_endpoint_snapshots_track_calls_independently() -> None:
    clock = FakeClock()
    limiter = JupiterRateLimiter(
        clock=clock.now,
        sleep=clock.sleep,
        quote_batch_window_seconds=0.0,
    )

    quote_future = limiter.submit(
        QUOTE_ENDPOINT,
        lambda: {"outAmount": "99"},
        priority=JupiterRequestPriority.SCANNING,
    )
    swap_future = limiter.submit(
        SWAP_ENDPOINT,
        lambda: {"swapTransaction": "abc"},
        priority=JupiterRequestPriority.EXECUTION,
    )

    assert limiter.run_until_idle() == 2
    assert swap_future.result() == {"swapTransaction": "abc"}
    assert quote_future.result() == {"outAmount": "99"}

    quote_stats = limiter.endpoint_snapshot(QUOTE_ENDPOINT)
    swap_stats = limiter.endpoint_snapshot(SWAP_ENDPOINT)

    assert quote_stats.completed_calls == 1
    assert quote_stats.calls_in_window == 1
    assert swap_stats.completed_calls == 1
    assert swap_stats.calls_in_window == 1
