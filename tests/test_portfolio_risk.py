import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.portfolio_risk import PortfolioRiskManager


class FakeFeed:
    def __init__(self, prices):
        self.history = deque(
            [SimpleNamespace(timestamp=index, price=price) for index, price in enumerate(prices, start=1)],
            maxlen=60,
        )
        self.current_price = prices[-1] if prices else None


def make_position(pair, entry_price, notional, status="open"):
    return SimpleNamespace(
        pair=pair,
        entry_price=entry_price,
        amount_sol=1.0,
        notional=notional,
        status=status,
    )


def test_calculates_total_exposure_and_pairwise_correlations():
    manager = PortfolioRiskManager()
    positions = [
        make_position("SOL/USDC", entry_price=100.0, notional=100.0),
        make_position("JUP/USDC", entry_price=50.0, notional=200.0),
    ]
    price_feeds = {
        "SOL/USDC": FakeFeed([100.0, 110.0, 99.0, 118.8]),
        "JUP/USDC": FakeFeed([50.0, 55.0, 49.5, 59.4]),
    }

    exposure = manager.calculate_total_exposure(positions, price_feeds=price_feeds)
    correlations = manager.calculate_position_correlations(positions, price_feeds)

    assert exposure == pytest.approx(356.4)
    assert correlations == {
        "SOL/USDC": {
            "SOL/USDC": pytest.approx(1.0),
            "JUP/USDC": pytest.approx(1.0),
        },
        "JUP/USDC": {
            "JUP/USDC": pytest.approx(1.0),
            "SOL/USDC": pytest.approx(1.0),
        },
    }
    assert manager.average_correlation(positions, price_feeds) == pytest.approx(1.0)


def test_drawdown_limit_halts_new_trading_until_reset():
    manager = PortfolioRiskManager(max_drawdown_pct=0.20)

    first = manager.update_drawdown(1_000.0, timestamp="2026-04-13T00:00:00")
    second = manager.update_drawdown(850.0, timestamp="2026-04-13T00:05:00")
    third = manager.update_drawdown(800.0, timestamp="2026-04-13T00:10:00")
    fourth = manager.update_drawdown(950.0, timestamp="2026-04-13T00:15:00")

    assert first["drawdown_pct"] == pytest.approx(0.0)
    assert second["drawdown_pct"] == pytest.approx(15.0)
    assert third["drawdown_pct"] == pytest.approx(20.0)
    assert fourth["drawdown_pct"] == pytest.approx(5.0)
    assert manager.halt_trading is True
    assert manager.can_open_new_positions() is False

    manager.reset_halt()

    assert manager.can_open_new_positions() is True


def test_snapshot_uses_kelly_sizing_and_ignores_non_realized_trade_records():
    manager = PortfolioRiskManager(kelly_fraction_cap=0.25)
    positions = [
        make_position("SOL/USDC", entry_price=100.0, notional=100.0),
        make_position("JUP/USDC", entry_price=50.0, notional=200.0),
    ]
    price_feeds = {
        "SOL/USDC": FakeFeed([100.0, 110.0, 99.0, 118.8]),
        "JUP/USDC": FakeFeed([50.0, 55.0, 49.5, 59.4]),
    }
    trade_history = [
        {"pnl_pct": 10.0},
        {"action": {"pnl_pct": 20.0}},
        SimpleNamespace(pnl_pct=-10.0),
        {"type": "OPEN", "status": "success"},
    ]

    snapshot = manager.portfolio_snapshot(
        positions,
        price_feeds=price_feeds,
        trade_history=trade_history,
        portfolio_value_usd=1_000.0,
        timestamp="2026-04-13T00:00:00",
    )

    assert snapshot["win_rate_pct"] == pytest.approx(66.6666666667)
    assert snapshot["avg_win_pct"] == pytest.approx(15.0)
    assert snapshot["avg_loss_pct"] == pytest.approx(10.0)
    assert snapshot["win_loss_ratio"] == pytest.approx(1.5)
    assert snapshot["kelly_fraction"] == pytest.approx(0.4444444444)
    assert snapshot["capped_kelly_fraction"] == pytest.approx(0.25)
    assert snapshot["average_correlation"] == pytest.approx(1.0)
    assert snapshot["adjusted_kelly_fraction"] == pytest.approx(0.125)
    assert snapshot["total_exposure_usd"] == pytest.approx(356.4)
    assert snapshot["available_exposure_usd"] == pytest.approx(643.6)
    assert snapshot["recommended_position_usd"] == pytest.approx(125.0)
    assert snapshot["halt_trading"] is False
