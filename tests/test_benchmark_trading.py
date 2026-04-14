from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_trading import (
    BenchmarkConfig,
    build_report_payload,
    render_report,
    run_benchmark,
    write_report_artifacts,
)


def make_config(trade_count: int = 40) -> BenchmarkConfig:
    return BenchmarkConfig(
        trade_count=trade_count,
        seed=11,
        stop_loss_pct=0.05,
        take_profit_pct=0.15,
        min_notional_usd=100.0,
        max_notional_usd=1_000.0,
        min_horizon_bars=24,
        max_horizon_bars=72,
        slippage_tolerance_bps=50,
    )


def test_build_report_payload_reconciles_theory_gap_components():
    run = run_benchmark(make_config())
    payload = build_report_payload(run)

    assert payload["outcomes"]["trade_count"] == 40
    assert sum(payload["outcomes"]["exit_reason_counts"].values()) == 40
    assert len(payload["trades"]) == 40
    assert len(payload["worst_trades"]) == 10

    leakage = payload["leakage"]
    reconstructed_gap = (
        leakage["trigger_regret_total_usd"]
        + leakage["slippage_drag_total_usd"]
        + leakage["fee_drag_total_usd"]
    )
    assert math.isclose(
        leakage["total_gap_usd"],
        reconstructed_gap,
        rel_tol=1e-9,
        abs_tol=1e-9,
    )

    assert payload["slippage"]["slippage_mae_bps"] >= 0.0
    assert payload["fees"]["total_fee_cost_usd"] > 0.0
    assert "stop_loss" in payload["hindsight"]
    assert "take_profit" in payload["hindsight"]


def test_render_report_mentions_hindsight_and_loss_sections():
    payload = build_report_payload(run_benchmark(make_config(trade_count=25)))
    report = render_report(payload)

    assert report.startswith("# Jupiter Trading Benchmark Report")
    assert "## Stop-Loss Vs Take-Profit Against Hindsight" in report
    assert "## Where Money Was Lost Vs Theory" in report
    assert "Trigger regret" in report
    assert "Slippage drag" in report
    assert "Fee drag" in report


def test_write_report_artifacts_creates_timestamped_and_latest_files(tmp_path: Path):
    payload = build_report_payload(run_benchmark(make_config(trade_count=20)))
    markdown_path, json_path, latest_markdown_path, latest_json_path = write_report_artifacts(
        payload,
        tmp_path,
    )

    assert markdown_path.exists()
    assert json_path.exists()
    assert latest_markdown_path.exists()
    assert latest_json_path.exists()
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Jupiter Trading Benchmark Report"
    )

    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["outcomes"]["trade_count"] == 20
    assert "pair_breakdown" in loaded
