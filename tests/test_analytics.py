import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics import TradingAnalytics


def test_track_execution_normalizes_swap_fields():
    analytics = TradingAnalytics()

    execution = analytics.track_execution(
        {
            "timestamp": "2024-04-12T09:30:00",
            "input_mint": "mint-in",
            "output_mint": "mint-out",
            "amount": 1_000_000,
            "status": "success",
            "out_amount": 950_000,
            "out_usd": 12.34,
            "price_impact": 0.42,
            "tx_signature": "abc123",
            "route_plan": [{"swapInfo": "kept in metadata"}],
        }
    )

    assert execution.status == "success"
    assert execution.out_usd == pytest.approx(12.34)
    assert execution.metadata == {"route_plan": [{"swapInfo": "kept in metadata"}]}
    assert analytics.summary()["tracked_executions"] == 1


def test_record_closed_position_normalizes_risk_manager_payload():
    analytics = TradingAnalytics()
    position = SimpleNamespace(
        pair="JUP/USDC",
        entry_time=1_712_952_000.0,
        entry_price=1.25,
        amount_sol=1.5,
    )

    trade = analytics.record_closed_position(
        {
            "position": position,
            "action": {"type": "TAKE_PROFIT", "price": 1.5, "pnl_pct": 20.0},
            "timestamp": "2024-04-13T18:30:00",
            "notional": 50.0,
        }
    )

    assert trade.pair == "JUP/USDC"
    assert trade.entry_price == pytest.approx(1.25)
    assert trade.exit_price == pytest.approx(1.5)
    assert trade.pnl_pct == pytest.approx(20.0)
    assert trade.pnl_amount == pytest.approx(10.0)
    assert trade.metadata["action_type"] == "TAKE_PROFIT"


def test_performance_metrics_use_compounded_daily_returns():
    analytics = TradingAnalytics(starting_equity=100.0)
    analytics.record_trade(
        "SOL/USDC",
        10.0,
        opened_at="2024-04-12T09:00:00",
        closed_at="2024-04-12T10:00:00",
        notional=100.0,
    )
    analytics.record_trade(
        "JUP/USDC",
        -5.0,
        opened_at="2024-04-12T11:00:00",
        closed_at="2024-04-12T12:00:00",
        notional=100.0,
    )
    analytics.record_trade(
        "BONK/USDC",
        20.0,
        opened_at="2024-04-13T09:00:00",
        closed_at="2024-04-13T12:00:00",
        notional=50.0,
    )
    analytics.record_trade(
        "WIF/USDC",
        -10.0,
        opened_at="2024-04-14T09:00:00",
        closed_at="2024-04-14T12:00:00",
        notional=80.0,
    )

    rows = analytics.daily_pnl()

    assert rows == [
        {
            "date": "2024-04-12",
            "trade_count": 2,
            "wins": 1,
            "losses": 1,
            "flats": 0,
            "realized_pnl": pytest.approx(5.0),
            "return_pct": pytest.approx(4.5),
            "avg_trade_return_pct": pytest.approx(2.5),
            "cumulative_return_pct": pytest.approx(4.5),
        },
        {
            "date": "2024-04-13",
            "trade_count": 1,
            "wins": 1,
            "losses": 0,
            "flats": 0,
            "realized_pnl": pytest.approx(10.0),
            "return_pct": pytest.approx(20.0),
            "avg_trade_return_pct": pytest.approx(20.0),
            "cumulative_return_pct": pytest.approx(25.4),
        },
        {
            "date": "2024-04-14",
            "trade_count": 1,
            "wins": 0,
            "losses": 1,
            "flats": 0,
            "realized_pnl": pytest.approx(-8.0),
            "return_pct": pytest.approx(-10.0),
            "avg_trade_return_pct": pytest.approx(-10.0),
            "cumulative_return_pct": pytest.approx(12.86),
        },
    ]
    assert analytics.calculate_win_rate() == pytest.approx(50.0)
    assert analytics.calculate_sharpe_ratio() == pytest.approx(6.154907219681128)
    assert analytics.calculate_max_drawdown() == pytest.approx(10.0)
    assert analytics.equity_curve() == [
        {"date": "2024-04-12", "equity": pytest.approx(104.5), "return_pct": pytest.approx(4.5)},
        {"date": "2024-04-13", "equity": pytest.approx(125.4), "return_pct": pytest.approx(20.0)},
        {"date": "2024-04-14", "equity": pytest.approx(112.86), "return_pct": pytest.approx(-10.0)},
    ]


def test_generate_daily_pnl_report_returns_markdown():
    analytics = TradingAnalytics()
    analytics.record_trade(
        "SOL/USDC",
        10.0,
        opened_at="2024-04-12T09:00:00",
        closed_at="2024-04-12T10:00:00",
        notional=100.0,
    )
    analytics.record_trade(
        "JUP/USDC",
        -5.0,
        opened_at="2024-04-12T11:00:00",
        closed_at="2024-04-12T12:00:00",
        notional=100.0,
    )

    report = analytics.generate_daily_pnl_report()

    assert report.startswith("# Daily P&L Report")
    assert "Period: 2024-04-12 -> 2024-04-12" in report
    assert "| 2024-04-12 | 2 | 1 | 1 | $5.00 | +4.50% | +4.50% |" in report
    assert "- Win rate: 50.00%" in report
    assert "- Realized P&L: $5.00" in report


def test_generate_daily_pnl_report_handles_empty_history():
    analytics = TradingAnalytics()

    assert analytics.generate_daily_pnl_report() == "# Daily P&L Report\n\nNo realized trades tracked."
