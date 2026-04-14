from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_oracle import (
    BenchmarkConfig,
    PhaseSummary,
    build_report_payload,
    parse_intervals,
    render_report,
    write_report_artifacts,
)


def make_config() -> BenchmarkConfig:
    return BenchmarkConfig(
        base_url="https://api.jup.ag/swap/v1",
        input_mint="So11111111111111111111111111111111111111112",
        output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount=1_000_000,
        slippage_bps=50,
        timeout_seconds=10.0,
        latency_samples=4,
        throughput_duration_seconds=20.0,
        throughput_concurrency=2,
        throughput_target_rps=1.0,
        rate_limit_probe_max_requests=20,
        rate_limit_probe_concurrency=4,
        reserve_execution_rpm=6,
        reserve_monitoring_rpm=12,
        reserve_metadata_rpm=4,
        safety_utilization=0.85,
        published_limit_rpm=60,
        monitor_intervals_seconds=(5, 10, 30, 60),
    )


def make_phase(
    name: str,
    *,
    requested: int,
    successes: int,
    rate_limited: int = 0,
    failures: int = 0,
    duration_seconds: float = 10.0,
    throughput_qps: float = 0.0,
    avg_response_time_ms: float | None = 100.0,
    sample_errors: tuple[str, ...] = (),
    first_rate_limited_at_request: int | None = None,
) -> PhaseSummary:
    return PhaseSummary(
        name=name,
        requested=requested,
        completed=successes + rate_limited + failures,
        successes=successes,
        rate_limited=rate_limited,
        failures=failures,
        duration_seconds=duration_seconds,
        achieved_throughput_qps=throughput_qps,
        avg_response_time_ms=avg_response_time_ms,
        avg_attempt_time_ms=avg_response_time_ms,
        p50_response_time_ms=avg_response_time_ms,
        p95_response_time_ms=avg_response_time_ms,
        p99_response_time_ms=avg_response_time_ms,
        min_response_time_ms=avg_response_time_ms,
        max_response_time_ms=avg_response_time_ms,
        avg_jupiter_time_taken=None,
        first_rate_limited_at_request=first_rate_limited_at_request,
        first_rate_limited_after_seconds=1.5 if first_rate_limited_at_request else None,
        max_retry_after_seconds=2.0 if first_rate_limited_at_request else None,
        sample_errors=sample_errors,
    )


def test_build_report_payload_computes_monitorable_tokens_from_measured_throughput():
    config = make_config()
    latency = make_phase("latency", requested=4, successes=4, throughput_qps=0.4)
    throughput = make_phase(
        "throughput",
        requested=20,
        successes=10,
        duration_seconds=20.0,
        throughput_qps=0.5,
    )
    probe = make_phase("rate_limit_probe", requested=20, successes=12, rate_limited=1)

    payload = build_report_payload(config, latency, throughput, probe)
    capacity = payload["monitoring_capacity"]

    assert capacity["basis"] == "measured_throughput_capped_by_published_bucket"
    assert capacity["observed_sustained_rpm"] == 30.0
    assert capacity["sustainable_budget_rpm"] == 30
    assert capacity["safe_budget_rpm"] == 25
    assert capacity["reserved_requests_per_minute"] == 22
    assert capacity["scan_budget_rpm"] == 3
    assert capacity["token_capacity_by_interval"] == {5: 0, 10: 0, 30: 1, 60: 3}


def test_render_report_mentions_fallback_when_no_live_successes():
    config = make_config()
    latency = make_phase(
        "latency",
        requested=4,
        successes=0,
        failures=4,
        avg_response_time_ms=None,
        sample_errors=("ConnectError: dns failure",),
    )
    throughput = make_phase(
        "throughput",
        requested=10,
        successes=0,
        failures=10,
        duration_seconds=10.0,
        throughput_qps=0.0,
        avg_response_time_ms=None,
        sample_errors=("ConnectError: dns failure",),
    )
    probe = make_phase(
        "rate_limit_probe",
        requested=20,
        successes=0,
        failures=20,
        duration_seconds=3.0,
        throughput_qps=0.0,
        avg_response_time_ms=None,
    )

    payload = build_report_payload(config, latency, throughput, probe)
    report = render_report(payload)

    assert "No successful live Jupiter quotes completed in this environment." in report
    assert "published_bucket_fallback" in report


def test_write_report_artifacts_creates_timestamped_and_latest_files(tmp_path: Path):
    config = make_config()
    latency = make_phase("latency", requested=4, successes=4, throughput_qps=0.4)
    throughput = make_phase(
        "throughput",
        requested=20,
        successes=10,
        duration_seconds=20.0,
        throughput_qps=0.5,
    )
    probe = make_phase("rate_limit_probe", requested=20, successes=12, rate_limited=1)

    payload = build_report_payload(config, latency, throughput, probe)
    markdown_path, json_path, latest_markdown_path, latest_json_path = write_report_artifacts(
        payload, tmp_path
    )

    assert markdown_path.exists()
    assert json_path.exists()
    assert latest_markdown_path.exists()
    assert latest_json_path.exists()
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Jupiter Oracle Benchmark Report"
    )
    assert json.loads(json_path.read_text(encoding="utf-8"))["monitoring_capacity"][
        "scan_budget_rpm"
    ] == 3


def test_parse_intervals_rejects_empty_and_non_positive_values():
    assert parse_intervals("5, 10,60") == (5, 10, 60)

    try:
        parse_intervals("0")
    except ValueError as exc:
        assert "positive integers" in str(exc)
    else:
        raise AssertionError("expected ValueError for zero interval")

    try:
        parse_intervals(" , ")
    except ValueError as exc:
        assert "at least one" in str(exc)
    else:
        raise AssertionError("expected ValueError for empty interval list")
