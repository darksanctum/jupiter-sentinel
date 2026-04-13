import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

fake_state_manager = types.ModuleType("src.state_manager")


class FakeStateManager:
    def __init__(self, *args, **kwargs):
        pass

    def get_locked_balance(self):
        return 0.0


fake_state_manager.DEFAULT_LOCK_PCT = 0.5
fake_state_manager.LOCK_PCT_ENV = "LOCK_PCT"
fake_state_manager.StateManager = FakeStateManager
sys.modules.setdefault("src.state_manager", fake_state_manager)

from src.backtest import (
    HistoricalBacktester,
    HistoricalPriceRow,
    format_strategy_comparison_report,
    generate_sample_rows,
    load_price_rows,
    render_equity_curve,
    run_parallel_backtests,
    write_backtest_report,
)


def make_rows():
    start = datetime(2024, 4, 12, 0, 0, 0)
    jup_prices = [1.00, 1.00, 0.99, 0.98, 0.96, 0.96, 0.98, 1.05, 1.12]
    rows = []

    for index, price in enumerate(jup_prices):
        rows.append(
            HistoricalPriceRow(
                timestamp=start + timedelta(minutes=index * 30),
                prices={
                    "SOL/USDC": 100.0,
                    "JUP/USDC": price,
                },
            )
        )

    return rows


def test_backtest_replays_alerts_and_generates_positive_equity_curve():
    result = HistoricalBacktester(make_rows(), starting_sol=10.0, entry_amount_sol=0.25, enter_on="down").run()

    assert result.summary["alerts"] >= 1
    assert result.summary["closed_trades"] == 1
    assert result.summary["win_rate"] == pytest.approx(100.0)
    assert result.trades[0]["pair"] == "JUP/USDC"
    assert result.trades[0]["action_type"] == "TAKE_PROFIT"
    assert result.trades[0]["pnl_pct"] == pytest.approx(16.666666666666664)
    assert result.equity_curve[0]["equity"] == pytest.approx(1000.0)
    assert result.equity_curve[-1]["equity"] > result.equity_curve[0]["equity"]


def test_load_price_rows_from_csv_and_render_curve(tmp_path):
    csv_path = tmp_path / "prices.csv"
    csv_path.write_text(
        "\n".join(
            [
                "timestamp,SOL/USDC,JUP/USDC",
                "2024-04-12T00:00:00,100,1.00",
                "2024-04-12T00:30:00,100,0.97",
            ]
        )
    )

    rows, source = load_price_rows(csv_path)
    chart = render_equity_curve(
        [
            {"timestamp": rows[0].timestamp.isoformat(), "equity": 1000.0},
            {"timestamp": rows[1].timestamp.isoformat(), "equity": 1001.0},
        ],
        width=8,
        height=4,
    )

    assert source.endswith("prices.csv")
    assert rows[0].prices["SOL/USDC"] == pytest.approx(100.0)
    assert "max $1001.00" in chart
    assert "*" in chart


def test_load_price_rows_from_directory_merges_pair_files(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    (data_dir / "sol_usdc.csv").write_text(
        "\n".join(
            [
                "timestamp,price",
                "2024-04-12T00:00:00,100",
                "2024-04-12T00:30:00,101",
            ]
        )
    )
    (data_dir / "jup_usdc.csv").write_text(
        "\n".join(
            [
                "timestamp,price",
                "2024-04-12T00:00:00,1.00",
                "2024-04-12T00:30:00,0.98",
            ]
        )
    )

    rows, source = load_price_rows(data_dir)

    assert "2 files" in source
    assert len(rows) == 2
    assert rows[0].prices["SOL/USDC"] == pytest.approx(100.0)
    assert rows[1].prices["JUP/USDC"] == pytest.approx(0.98)


def test_parallel_backtests_generate_markdown_report(tmp_path):
    results = run_parallel_backtests(generate_sample_rows(), starting_sol=10.0, entry_amount_sol=0.25)
    report = format_strategy_comparison_report(results, source="synthetic sample")
    report_path = write_backtest_report(report, tmp_path / "backtest_report.md")

    assert len(results) == 3
    assert report_path.exists()
    assert report_path.read_text().startswith("# Jupiter Sentinel Backtest Report")
    assert "volatility_reversal" in report
    assert "momentum" in report
    assert "mean_reversion" in report

    for result in results:
        assert "sharpe_ratio" in result.summary
        assert "sortino_ratio" in result.summary
        assert "max_drawdown_pct" in result.summary
        assert "win_rate" in result.summary
