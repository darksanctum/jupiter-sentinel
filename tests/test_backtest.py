import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest import (
    HistoricalBacktester,
    HistoricalPriceRow,
    load_price_rows,
    render_equity_curve,
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
