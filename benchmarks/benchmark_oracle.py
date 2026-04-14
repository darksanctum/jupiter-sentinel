#!/usr/bin/env python3
"""
Benchmark Jupiter quote API performance for the oracle use case.

Outputs:
- Timestamped Markdown report in benchmarks/results/
- Timestamped JSON payload in benchmarks/results/
- latest Markdown/JSON copies for easy inspection
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Sequence

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import HEADERS, JUPITER_SWAP_V1, SOL_MINT, USDC_MINT
from src.resilience import atomic_write_text
from src.validation import build_jupiter_quote_url

KEYLESS_REQUESTS_PER_MINUTE = 30
FREE_REQUESTS_PER_MINUTE = 60
DEFAULT_OUTPUT_DIR = ROOT / "benchmarks" / "results"


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    base_url: str
    input_mint: str
    output_mint: str
    amount: int
    slippage_bps: int
    timeout_seconds: float
    latency_samples: int
    throughput_duration_seconds: float
    throughput_concurrency: int
    throughput_target_rps: float
    rate_limit_probe_max_requests: int
    rate_limit_probe_concurrency: int
    reserve_execution_rpm: int
    reserve_monitoring_rpm: int
    reserve_metadata_rpm: int
    safety_utilization: float
    published_limit_rpm: int
    monitor_intervals_seconds: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RequestMeasurement:
    request_id: int
    started_at: float
    finished_at: float
    elapsed_seconds: float
    status_code: int | None
    ok: bool
    rate_limited: bool
    retry_after_seconds: float | None
    jupiter_time_taken: float | None
    error: str | None


@dataclass(frozen=True, slots=True)
class PhaseSummary:
    name: str
    requested: int
    completed: int
    successes: int
    rate_limited: int
    failures: int
    duration_seconds: float
    achieved_throughput_qps: float
    avg_response_time_ms: float | None
    avg_attempt_time_ms: float | None
    p50_response_time_ms: float | None
    p95_response_time_ms: float | None
    p99_response_time_ms: float | None
    min_response_time_ms: float | None
    max_response_time_ms: float | None
    avg_jupiter_time_taken: float | None
    first_rate_limited_at_request: int | None
    first_rate_limited_after_seconds: float | None
    max_retry_after_seconds: float | None
    sample_errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MonitoringCapacity:
    basis: str
    observed_sustained_rpm: float | None
    sustainable_budget_rpm: int
    safe_budget_rpm: int
    reserved_requests_per_minute: int
    scan_budget_rpm: int
    token_capacity_by_interval: dict[int, int]


def percentile(values: Sequence[float], value: float) -> float | None:
    """Return a simple linear-interpolated percentile."""
    if not values:
        return None

    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * value
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]

    fraction = rank - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def infer_published_limit_rpm() -> int:
    """Infer the published Jupiter bucket based on API key availability."""
    return FREE_REQUESTS_PER_MINUTE if "x-api-key" in HEADERS else KEYLESS_REQUESTS_PER_MINUTE


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as either seconds or an HTTP date."""
    if value in (None, ""):
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return max(0.0, float(text))
    except ValueError:
        pass

    try:
        retry_time = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None

    if retry_time.tzinfo is None:
        retry_time = retry_time.replace(tzinfo=timezone.utc)
    return max(0.0, retry_time.timestamp() - time.time())


def mean(values: Sequence[float]) -> float | None:
    """Return the mean when values are present."""
    if not values:
        return None
    return sum(values) / len(values)


def format_ms(value: float | None) -> str:
    """Render a millisecond measurement for Markdown output."""
    if value is None:
        return "n/a"
    return f"{value:.1f} ms"


def format_float(value: float | None, digits: int = 2) -> str:
    """Render a float for Markdown output."""
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def quote_url(config: BenchmarkConfig) -> str:
    """Build the exact quote URL used by the oracle path."""
    return build_jupiter_quote_url(
        config.base_url,
        config.input_mint,
        config.output_mint,
        config.amount,
        config.slippage_bps,
        swap_mode="ExactIn",
        restrict_intermediate_tokens=True,
    )


async def fetch_quote(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
    request_id: int,
) -> RequestMeasurement:
    """Perform one Jupiter quote request and capture timing and error details."""
    started_at = time.perf_counter()
    try:
        response = await client.get(url, headers=headers, timeout=timeout_seconds)
        finished_at = time.perf_counter()
        elapsed_seconds = finished_at - started_at

        retry_after_seconds = parse_retry_after(response.headers.get("Retry-After"))
        error: str | None = None
        jupiter_time_taken: float | None = None
        payload: Any = None

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            raw_time_taken = payload.get("timeTaken")
            if isinstance(raw_time_taken, (int, float)):
                jupiter_time_taken = float(raw_time_taken)
            elif isinstance(raw_time_taken, str):
                try:
                    jupiter_time_taken = float(raw_time_taken.strip())
                except ValueError:
                    jupiter_time_taken = None

            if response.status_code >= 400:
                for key in ("error", "message", "description"):
                    value = payload.get(key)
                    if value not in (None, ""):
                        error = str(value)
                        break

        if response.status_code >= 400 and error is None:
            error = f"HTTP {response.status_code}"

        ok = response.status_code == 200 and isinstance(payload, dict) and "outAmount" in payload
        return RequestMeasurement(
            request_id=request_id,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            status_code=response.status_code,
            ok=ok,
            rate_limited=response.status_code == 429,
            retry_after_seconds=retry_after_seconds,
            jupiter_time_taken=jupiter_time_taken,
            error=error if not ok else None,
        )
    except Exception as exc:
        finished_at = time.perf_counter()
        return RequestMeasurement(
            request_id=request_id,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=finished_at - started_at,
            status_code=None,
            ok=False,
            rate_limited=False,
            retry_after_seconds=None,
            jupiter_time_taken=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def summarize_phase(
    name: str,
    requested: int,
    measurements: Sequence[RequestMeasurement],
    duration_seconds: float,
    *,
    phase_started_at: float,
) -> PhaseSummary:
    """Aggregate request measurements into a stable report structure."""
    completed = len(measurements)
    successes = sum(1 for measurement in measurements if measurement.ok)
    rate_limited = sum(1 for measurement in measurements if measurement.rate_limited)
    failures = sum(
        1
        for measurement in measurements
        if not measurement.ok and not measurement.rate_limited
    )
    attempt_latencies_ms = [measurement.elapsed_seconds * 1000 for measurement in measurements]
    success_latencies_ms = [
        measurement.elapsed_seconds * 1000 for measurement in measurements if measurement.ok
    ]
    jupiter_time_taken_values = [
        measurement.jupiter_time_taken
        for measurement in measurements
        if measurement.jupiter_time_taken is not None
    ]
    retry_after_values = [
        measurement.retry_after_seconds
        for measurement in measurements
        if measurement.retry_after_seconds is not None
    ]
    first_rate_limited = min(
        (measurement for measurement in measurements if measurement.rate_limited),
        default=None,
        key=lambda measurement: measurement.request_id,
    )
    error_counter = Counter(
        measurement.error
        for measurement in measurements
        if measurement.error not in (None, "")
    )
    sample_errors = tuple(error for error, _ in error_counter.most_common(5))

    return PhaseSummary(
        name=name,
        requested=requested,
        completed=completed,
        successes=successes,
        rate_limited=rate_limited,
        failures=failures,
        duration_seconds=duration_seconds,
        achieved_throughput_qps=(successes / duration_seconds) if duration_seconds > 0 else 0.0,
        avg_response_time_ms=mean(success_latencies_ms),
        avg_attempt_time_ms=mean(attempt_latencies_ms),
        p50_response_time_ms=percentile(success_latencies_ms, 0.50),
        p95_response_time_ms=percentile(success_latencies_ms, 0.95),
        p99_response_time_ms=percentile(success_latencies_ms, 0.99),
        min_response_time_ms=min(success_latencies_ms) if success_latencies_ms else None,
        max_response_time_ms=max(success_latencies_ms) if success_latencies_ms else None,
        avg_jupiter_time_taken=mean(jupiter_time_taken_values),
        first_rate_limited_at_request=(
            first_rate_limited.request_id if first_rate_limited is not None else None
        ),
        first_rate_limited_after_seconds=(
            first_rate_limited.finished_at - phase_started_at
            if first_rate_limited is not None
            else None
        ),
        max_retry_after_seconds=max(retry_after_values) if retry_after_values else None,
        sample_errors=sample_errors,
    )


async def run_latency_phase(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
) -> PhaseSummary:
    """Measure single-request response time with sequential quotes."""
    measurements: list[RequestMeasurement] = []
    url = quote_url(config)
    phase_started_at = time.perf_counter()
    for request_id in range(1, config.latency_samples + 1):
        measurements.append(
            await fetch_quote(client, url, dict(HEADERS), config.timeout_seconds, request_id)
        )
    duration_seconds = time.perf_counter() - phase_started_at
    return summarize_phase(
        "latency",
        config.latency_samples,
        measurements,
        duration_seconds,
        phase_started_at=phase_started_at,
    )


async def run_throughput_phase(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
) -> PhaseSummary:
    """Measure steady-state quotes/second at a configured target rate."""
    url = quote_url(config)
    measurements: list[RequestMeasurement] = []
    semaphore = asyncio.Semaphore(config.throughput_concurrency)
    phase_started_at = time.perf_counter()
    phase_deadline = phase_started_at + config.throughput_duration_seconds
    tasks: list[asyncio.Task[RequestMeasurement]] = []

    async def limited_fetch(request_id: int) -> RequestMeasurement:
        async with semaphore:
            return await fetch_quote(
                client, url, dict(HEADERS), config.timeout_seconds, request_id
            )

    request_id = 0
    next_dispatch_at = phase_started_at
    while next_dispatch_at < phase_deadline or request_id == 0:
        now = time.perf_counter()
        if now < next_dispatch_at:
            await asyncio.sleep(next_dispatch_at - now)
        request_id += 1
        tasks.append(asyncio.create_task(limited_fetch(request_id)))
        next_dispatch_at = phase_started_at + (request_id / config.throughput_target_rps)

    if tasks:
        measurements = await asyncio.gather(*tasks)
    duration_seconds = time.perf_counter() - phase_started_at
    return summarize_phase(
        "throughput",
        request_id,
        measurements,
        duration_seconds,
        phase_started_at=phase_started_at,
    )


async def run_rate_limit_probe(
    client: httpx.AsyncClient,
    config: BenchmarkConfig,
) -> PhaseSummary:
    """Burst until a 429 appears or the configured request cap is exhausted."""
    url = quote_url(config)
    phase_started_at = time.perf_counter()
    measurements: list[RequestMeasurement] = []
    stop_after_rate_limit = asyncio.Event()
    results_lock = asyncio.Lock()
    counter_lock = asyncio.Lock()
    next_request_id = 1

    async def take_request_id() -> int | None:
        nonlocal next_request_id
        async with counter_lock:
            if stop_after_rate_limit.is_set():
                return None
            if next_request_id > config.rate_limit_probe_max_requests:
                return None
            current = next_request_id
            next_request_id += 1
            return current

    async def worker() -> None:
        while True:
            request_id = await take_request_id()
            if request_id is None:
                return
            measurement = await fetch_quote(
                client, url, dict(HEADERS), config.timeout_seconds, request_id
            )
            async with results_lock:
                measurements.append(measurement)
            if measurement.rate_limited:
                stop_after_rate_limit.set()

    await asyncio.gather(
        *(worker() for _ in range(config.rate_limit_probe_concurrency))
    )
    duration_seconds = time.perf_counter() - phase_started_at
    ordered_measurements = sorted(measurements, key=lambda item: item.request_id)
    requested = next_request_id - 1
    return summarize_phase(
        "rate_limit_probe",
        requested,
        ordered_measurements,
        duration_seconds,
        phase_started_at=phase_started_at,
    )


def compute_monitoring_capacity(
    config: BenchmarkConfig,
    throughput: PhaseSummary,
    latency: PhaseSummary,
) -> MonitoringCapacity:
    """
    Estimate how many tokens can be monitored under a steady-state budget.

    This deliberately models one quote per token per interval. If a strategy
    needs multiple quote sizes or extra metadata calls per token, the real
    capacity will be lower.
    """
    reserved_requests_per_minute = (
        config.reserve_execution_rpm
        + config.reserve_monitoring_rpm
        + config.reserve_metadata_rpm
    )

    observed_sustained_rpm: float | None = None
    basis = "published_bucket_fallback"
    sustainable_budget_rpm = config.published_limit_rpm

    if throughput.successes > 0 and throughput.duration_seconds > 0:
        observed_sustained_rpm = throughput.achieved_throughput_qps * 60.0
        sustainable_budget_rpm = max(
            1, min(config.published_limit_rpm, math.floor(observed_sustained_rpm))
        )
        basis = "measured_throughput_capped_by_published_bucket"
    elif latency.successes > 0:
        basis = "published_bucket_fallback_with_live_latency_only"

    safe_budget_rpm = max(1, math.floor(sustainable_budget_rpm * config.safety_utilization))
    scan_budget_rpm = max(0, safe_budget_rpm - reserved_requests_per_minute)
    token_capacity_by_interval = {
        interval_seconds: max(0, math.floor((scan_budget_rpm * interval_seconds) / 60))
        for interval_seconds in config.monitor_intervals_seconds
    }

    return MonitoringCapacity(
        basis=basis,
        observed_sustained_rpm=observed_sustained_rpm,
        sustainable_budget_rpm=sustainable_budget_rpm,
        safe_budget_rpm=safe_budget_rpm,
        reserved_requests_per_minute=reserved_requests_per_minute,
        scan_budget_rpm=scan_budget_rpm,
        token_capacity_by_interval=token_capacity_by_interval,
    )


def connectivity_status(*phases: PhaseSummary) -> str:
    """Summarize whether the run had live successes."""
    if any(phase.successes > 0 for phase in phases):
        return "live_success"
    if any(phase.completed > 0 for phase in phases):
        return "attempted_without_success"
    return "not_attempted"


def build_report_payload(
    config: BenchmarkConfig,
    latency: PhaseSummary,
    throughput: PhaseSummary,
    rate_limit_probe: PhaseSummary,
) -> dict[str, Any]:
    """Assemble the full JSON payload written to disk."""
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    capacity = compute_monitoring_capacity(config, throughput, latency)
    status = connectivity_status(latency, throughput, rate_limit_probe)
    return {
        "generated_at": generated_at,
        "connectivity_status": status,
        "config": asdict(config),
        "phases": {
            "latency": asdict(latency),
            "throughput": asdict(throughput),
            "rate_limit_probe": asdict(rate_limit_probe),
        },
        "monitoring_capacity": asdict(capacity),
        "quote_url": quote_url(config),
        "headers_used": {
            key: ("<redacted>" if key.lower() == "x-api-key" else value)
            for key, value in HEADERS.items()
        },
    }


def render_report(payload: dict[str, Any]) -> str:
    """Render a human-readable Markdown report."""
    config = payload["config"]
    latency = payload["phases"]["latency"]
    throughput = payload["phases"]["throughput"]
    rate_limit_probe = payload["phases"]["rate_limit_probe"]
    capacity = payload["monitoring_capacity"]
    generated_at = payload["generated_at"]

    notes: list[str] = []
    if payload["connectivity_status"] != "live_success":
        notes.append(
            "No successful live Jupiter quotes completed in this environment. Monitoring capacity is modeled from the published bucket, not measured end-to-end network performance."
        )
    if rate_limit_probe["rate_limited"] == 0:
        notes.append(
            "The rate-limit probe did not observe a 429 before reaching its request cap."
        )
    elif rate_limit_probe["max_retry_after_seconds"] is not None:
        notes.append(
            f"Observed Retry-After up to {rate_limit_probe['max_retry_after_seconds']:.2f} seconds during the rate-limit probe."
        )
    if throughput["sample_errors"]:
        notes.append(
            f"Throughput phase errors: {', '.join(throughput['sample_errors'])}"
        )

    lines = [
        "# Jupiter Oracle Benchmark Report",
        "",
        f"- Generated: {generated_at}",
        f"- Connectivity status: {payload['connectivity_status']}",
        f"- Published Jupiter bucket: {config['published_limit_rpm']} requests/minute",
        f"- API key present: {'yes' if 'x-api-key' in HEADERS else 'no'}",
        f"- Oracle quote: `{payload['quote_url']}`",
        "",
        "## Summary",
        "",
        "| Metric | Latency Phase | Throughput Phase | Rate-Limit Probe |",
        "| --- | ---: | ---: | ---: |",
        f"| Requests attempted | {latency['requested']} | {throughput['requested']} | {rate_limit_probe['requested']} |",
        f"| Successes | {latency['successes']} | {throughput['successes']} | {rate_limit_probe['successes']} |",
        f"| 429 responses | {latency['rate_limited']} | {throughput['rate_limited']} | {rate_limit_probe['rate_limited']} |",
        f"| Failures | {latency['failures']} | {throughput['failures']} | {rate_limit_probe['failures']} |",
        f"| Avg response time | {format_ms(latency['avg_response_time_ms'])} | {format_ms(throughput['avg_response_time_ms'])} | {format_ms(rate_limit_probe['avg_response_time_ms'])} |",
        f"| p95 response time | {format_ms(latency['p95_response_time_ms'])} | {format_ms(throughput['p95_response_time_ms'])} | {format_ms(rate_limit_probe['p95_response_time_ms'])} |",
        f"| Throughput | {format_float(latency['achieved_throughput_qps'])} qps | {format_float(throughput['achieved_throughput_qps'])} qps | {format_float(rate_limit_probe['achieved_throughput_qps'])} qps |",
        "",
        "## Rate-Limit Behavior",
        "",
        f"- First 429 at request: {rate_limit_probe['first_rate_limited_at_request'] or 'n/a'}",
        f"- First 429 after: {format_float(rate_limit_probe['first_rate_limited_after_seconds'])} seconds",
        f"- Max Retry-After observed: {format_float(rate_limit_probe['max_retry_after_seconds'])} seconds",
        "",
        "## Realistic Monitoring Capacity",
        "",
        f"- Capacity basis: `{capacity['basis']}`",
        f"- Observed sustained throughput: {format_float(capacity['observed_sustained_rpm'])} requests/minute",
        f"- Sustainable bucket used: {capacity['sustainable_budget_rpm']} requests/minute",
        f"- Safe bucket after {int(config['safety_utilization'] * 100)}% utilization: {capacity['safe_budget_rpm']} requests/minute",
        f"- Reserved for execution + monitoring + metadata: {capacity['reserved_requests_per_minute']} requests/minute",
        f"- Remaining scan budget: {capacity['scan_budget_rpm']} requests/minute",
        "",
        "| Monitoring Interval | Realistic Tokens Monitored |",
        "| --- | ---: |",
    ]

    for interval_seconds, token_capacity in capacity["token_capacity_by_interval"].items():
        label = f"{interval_seconds} sec"
        if interval_seconds % 60 == 0:
            label = f"{interval_seconds // 60} min"
        lines.append(f"| {label} | {token_capacity} |")

    lines.extend(
        [
            "",
            "Assumption: 1 Jupiter quote per token per cycle against a common quote asset. Multi-size depth scans or extra metadata calls reduce the usable token count.",
        ]
    )

    if notes:
        lines.extend(["", "## Notes", ""])
        for note in notes:
            lines.append(f"- {note}")

    return "\n".join(lines) + "\n"


def write_report_artifacts(
    payload: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    """Persist timestamped and latest report artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    markdown = render_report(payload)
    json_payload = json.dumps(payload, indent=2, sort_keys=True)

    markdown_path = output_dir / f"oracle_benchmark_{timestamp}.md"
    json_path = output_dir / f"oracle_benchmark_{timestamp}.json"
    latest_markdown_path = output_dir / "oracle_benchmark_latest.md"
    latest_json_path = output_dir / "oracle_benchmark_latest.json"

    atomic_write_text(markdown_path, markdown, encoding="utf-8")
    atomic_write_text(json_path, json_payload, encoding="utf-8")
    atomic_write_text(latest_markdown_path, markdown, encoding="utf-8")
    atomic_write_text(latest_json_path, json_payload, encoding="utf-8")

    return markdown_path, json_path, latest_markdown_path, latest_json_path


def parse_intervals(value: str) -> tuple[int, ...]:
    """Parse a comma-separated list of positive interval seconds."""
    intervals: list[int] = []
    for chunk in value.split(","):
        normalized = chunk.strip()
        if not normalized:
            continue
        interval = int(normalized)
        if interval <= 0:
            raise ValueError("monitor intervals must be positive integers")
        intervals.append(interval)
    if not intervals:
        raise ValueError("at least one monitoring interval is required")
    return tuple(intervals)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI for the benchmark runner."""
    published_limit_rpm = infer_published_limit_rpm()
    parser = argparse.ArgumentParser(
        description="Benchmark Jupiter quote API performance for the oracle path."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for benchmark results.",
    )
    parser.add_argument("--base-url", default=JUPITER_SWAP_V1)
    parser.add_argument("--input-mint", default=SOL_MINT)
    parser.add_argument("--output-mint", default=USDC_MINT)
    parser.add_argument(
        "--amount",
        type=int,
        default=1_000_000,
        help="Raw token amount used for the oracle quote.",
    )
    parser.add_argument("--slippage-bps", type=int, default=50)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--latency-samples", type=int, default=6)
    parser.add_argument("--throughput-duration-seconds", type=float, default=20.0)
    parser.add_argument("--throughput-concurrency", type=int, default=2)
    parser.add_argument(
        "--throughput-target-rps",
        type=float,
        default=(published_limit_rpm / 60.0),
        help="Steady-state request rate for the throughput phase.",
    )
    parser.add_argument(
        "--rate-limit-probe-max-requests",
        type=int,
        default=max(10, published_limit_rpm * 2),
    )
    parser.add_argument("--rate-limit-probe-concurrency", type=int, default=8)
    parser.add_argument("--reserve-execution-rpm", type=int, default=6)
    parser.add_argument("--reserve-monitoring-rpm", type=int, default=12)
    parser.add_argument("--reserve-metadata-rpm", type=int, default=4)
    parser.add_argument("--safety-utilization", type=float, default=0.85)
    parser.add_argument(
        "--published-limit-rpm",
        type=int,
        default=published_limit_rpm,
        help="Override the published Jupiter request bucket used for capacity calculations.",
    )
    parser.add_argument(
        "--monitor-intervals",
        type=parse_intervals,
        default=parse_intervals("5,10,30,60,300"),
        help="Comma-separated monitoring intervals in seconds.",
    )
    return parser


async def async_main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    config = BenchmarkConfig(
        base_url=args.base_url,
        input_mint=args.input_mint,
        output_mint=args.output_mint,
        amount=args.amount,
        slippage_bps=args.slippage_bps,
        timeout_seconds=args.timeout_seconds,
        latency_samples=args.latency_samples,
        throughput_duration_seconds=args.throughput_duration_seconds,
        throughput_concurrency=args.throughput_concurrency,
        throughput_target_rps=args.throughput_target_rps,
        rate_limit_probe_max_requests=args.rate_limit_probe_max_requests,
        rate_limit_probe_concurrency=args.rate_limit_probe_concurrency,
        reserve_execution_rpm=args.reserve_execution_rpm,
        reserve_monitoring_rpm=args.reserve_monitoring_rpm,
        reserve_metadata_rpm=args.reserve_metadata_rpm,
        safety_utilization=args.safety_utilization,
        published_limit_rpm=args.published_limit_rpm,
        monitor_intervals_seconds=args.monitor_intervals,
    )

    async with httpx.AsyncClient(http2=True) as client:
        latency = await run_latency_phase(client, config)
        throughput = await run_throughput_phase(client, config)
        rate_limit_probe = await run_rate_limit_probe(client, config)

    payload = build_report_payload(config, latency, throughput, rate_limit_probe)
    markdown_path, json_path, latest_markdown_path, latest_json_path = write_report_artifacts(
        payload, args.output_dir
    )

    print(render_report(payload))
    print(f"Markdown report: {markdown_path}")
    print(f"JSON report: {json_path}")
    print(f"Latest Markdown: {latest_markdown_path}")
    print(f"Latest JSON: {latest_json_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Synchronous CLI wrapper."""
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
