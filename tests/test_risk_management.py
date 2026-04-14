import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.risk as risk
from src.oracle import PricePoint
from src.portfolio_risk import PortfolioRiskManager
from src.risk import Position, RiskManager


class FakeExecutor:
    def __init__(self, balance=None):
        self.balance = balance or {"sol": 10.0, "sol_price": 100.0, "usd_value": 1_000.0}

    def get_balance(self):
        return self.balance


class StaticEntryFeed:
    def __init__(self, price):
        self.price = price
        self.current_price = price

    def fetch_price(self):
        return PricePoint(timestamp=1.0, price=self.price)


class SequenceFeed:
    def __init__(self, prices):
        self.prices = list(prices)
        self.index = 0
        self.current_price = self.prices[0] if self.prices else None

    def fetch_price(self):
        if self.index >= len(self.prices):
            price = self.prices[-1]
        else:
            price = self.prices[self.index]
            self.index += 1
        self.current_price = price
        return PricePoint(timestamp=float(self.index), price=price)


class HistoryFeed:
    def __init__(self, prices):
        self.history = deque(
            [
                SimpleNamespace(timestamp=float(index), price=price)
                for index, price in enumerate(prices, start=1)
            ],
            maxlen=60,
        )
        self.current_price = prices[-1] if prices else None


def make_position(
    *,
    pair="JUP/USDC",
    entry_price=100.0,
    amount_sol=1.0,
    notional=None,
    stop_loss_pct=0.05,
    take_profit_pct=0.15,
    trailing_stop_pct=0.03,
    highest_price=None,
    status="open",
):
    return Position(
        pair=pair,
        input_mint="input-mint",
        output_mint="output-mint",
        entry_price=entry_price,
        amount_sol=amount_sol,
        entry_time=1.0,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
        highest_price=entry_price if highest_price is None else highest_price,
        status=status,
        notional=entry_price * amount_sol if notional is None else notional,
    )


@pytest.mark.parametrize(
    ("requested_amount_sol", "sol_price", "tradable_sol", "expected_amount_sol"),
    [
        (100.0, 10.0, 10.0, 0.5),
        (100.0, 1.0, 0.4, 0.32),
        (0.2, 10.0, 10.0, 0.2),
    ],
)
def test_position_sizing_never_exceeds_configured_limits(
    monkeypatch,
    requested_amount_sol,
    sol_price,
    tradable_sol,
    expected_amount_sol,
):
    executor = FakeExecutor(
        balance={"sol": 50.0, "sol_price": sol_price, "usd_value": 50.0 * sol_price}
    )
    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: StaticEntryFeed(price=1.25))
    monkeypatch.setattr(
        risk,
        "get_tradable_balance",
        lambda total_balance, **kwargs: tradable_sol,
    )

    manager = RiskManager(executor)
    position = manager.open_position(
        pair="JUP/USDC",
        input_mint="input-mint",
        output_mint="output-mint",
        amount_sol=requested_amount_sol,
        dry_run=True,
    )

    assert position is not None
    assert position.amount_sol == pytest.approx(expected_amount_sol)
    assert position.amount_sol <= requested_amount_sol
    assert position.amount_sol <= tradable_sol * 0.8 + 1e-12
    assert position.notional <= risk.MAX_POSITION_USD + 1e-12
    assert position.amount_sol <= (risk.MAX_POSITION_USD / sol_price) + 1e-12


def test_stop_loss_triggers_at_exact_threshold_price():
    manager = RiskManager(FakeExecutor())
    position = make_position(stop_loss_pct=0.05, take_profit_pct=0.5)
    manager.positions = [position]
    manager.price_feeds = {position.pair: SequenceFeed([95.0])}

    actions = manager.check_positions()

    assert actions == [
        {
            "type": "STOP_LOSS",
            "pair": position.pair,
            "pnl_pct": pytest.approx(-5.0),
            "price": pytest.approx(95.0),
        }
    ]
    assert manager.positions == []
    assert position.status == "closed"


def test_take_profit_triggers_at_exact_threshold_price():
    manager = RiskManager(FakeExecutor())
    position = make_position(stop_loss_pct=0.2, take_profit_pct=0.15)
    manager.positions = [position]
    manager.price_feeds = {position.pair: SequenceFeed([115.0])}

    actions = manager.check_positions()

    assert actions == [
        {
            "type": "TAKE_PROFIT",
            "pair": position.pair,
            "pnl_pct": pytest.approx(15.0),
            "price": pytest.approx(115.0),
        }
    ]
    assert manager.positions == []
    assert position.status == "closed"


def test_trailing_stop_triggers_at_exact_trail_after_new_high():
    manager = RiskManager(FakeExecutor())
    position = make_position(
        take_profit_pct=0.50,
        trailing_stop_pct=0.03,
        highest_price=100.0,
    )
    trailing_stop_price = 110.0 * (1 - position.trailing_stop_pct)
    manager.positions = [position]
    manager.price_feeds = {position.pair: SequenceFeed([110.0, trailing_stop_price])}

    assert manager.check_positions() == []
    assert position.highest_price == pytest.approx(110.0)

    actions = manager.check_positions()

    assert actions == [
        {
            "type": "TRAILING_STOP",
            "pair": position.pair,
            "pnl_pct": pytest.approx((trailing_stop_price - 100.0) / 100.0 * 100.0),
            "price": pytest.approx(trailing_stop_price),
            "highest": pytest.approx(110.0),
        }
    ]
    assert manager.positions == []
    assert position.status == "closed"


def test_portfolio_exposure_sizing_is_capped_by_remaining_headroom():
    manager = PortfolioRiskManager(
        max_drawdown_pct=0.20,
        max_portfolio_exposure_pct=0.25,
        kelly_fraction_cap=1.0,
    )
    positions = [make_position(pair="SOL/USDC", entry_price=1.0, amount_sol=240.0, notional=240.0)]
    price_feeds = {"SOL/USDC": HistoryFeed([1.0, 1.0, 1.0])}
    trade_history = [{"pnl_pct": 20.0}, {"pnl_pct": -5.0}]

    sizing = manager.recommend_position_size(
        1_000.0,
        trade_history,
        positions=positions,
        price_feeds=price_feeds,
    )

    assert sizing["current_exposure_usd"] == pytest.approx(240.0)
    assert sizing["available_exposure_usd"] == pytest.approx(10.0)
    assert sizing["recommended_position_usd"] == pytest.approx(10.0)
    assert sizing["recommended_position_usd"] <= sizing["available_exposure_usd"] + 1e-12


def test_drawdown_halt_is_exact_sticky_and_zeroes_new_size_recommendations():
    manager = PortfolioRiskManager(
        max_drawdown_pct=0.20,
        max_portfolio_exposure_pct=0.50,
        kelly_fraction_cap=1.0,
    )
    positions = [make_position(pair="SOL/USDC", entry_price=1.0, amount_sol=100.0, notional=100.0)]
    price_feeds = {"SOL/USDC": HistoryFeed([1.0, 1.0, 1.0])}
    trade_history = [{"pnl_pct": 20.0}, {"pnl_pct": -5.0}]

    baseline = manager.update_drawdown(1_000.0, timestamp="2026-04-13T00:00:00")
    breached = manager.update_drawdown(800.0, timestamp="2026-04-13T00:05:00")
    recovered = manager.update_drawdown(950.0, timestamp="2026-04-13T00:10:00")
    sizing = manager.recommend_position_size(
        1_000.0,
        trade_history,
        positions=positions,
        price_feeds=price_feeds,
    )

    assert baseline["drawdown_pct"] == pytest.approx(0.0)
    assert breached["drawdown_pct"] == pytest.approx(20.0)
    assert recovered["drawdown_pct"] == pytest.approx(5.0)
    assert manager.halt_trading is True
    assert manager.can_open_new_positions() is False
    assert sizing["recommended_position_usd"] == pytest.approx(0.0)


def test_full_risk_pipeline_uses_tighter_portfolio_and_position_caps(monkeypatch):
    sol_price = 2.0
    portfolio_manager = PortfolioRiskManager(
        max_drawdown_pct=0.20,
        max_portfolio_exposure_pct=0.004,
        kelly_fraction_cap=1.0,
    )
    trade_history = [{"pnl_pct": 20.0}, {"pnl_pct": -5.0}]
    portfolio_size = portfolio_manager.recommend_position_size(
        1_000.0,
        trade_history,
        positions=[],
        price_feeds={},
    )["recommended_position_usd"]

    executor = FakeExecutor(
        balance={"sol": 100.0, "sol_price": sol_price, "usd_value": 200.0}
    )
    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: StaticEntryFeed(price=1.0))
    monkeypatch.setattr(
        risk,
        "get_tradable_balance",
        lambda total_balance, **kwargs: total_balance,
    )

    manager = RiskManager(executor)
    position = manager.open_position(
        pair="JUP/USDC",
        input_mint="input-mint",
        output_mint="output-mint",
        amount_sol=portfolio_size / sol_price,
        dry_run=True,
    )

    assert portfolio_size == pytest.approx(4.0)
    assert position is not None
    assert position.notional == pytest.approx(4.0)
    assert position.notional <= portfolio_size + 1e-12
    assert position.notional <= risk.MAX_POSITION_USD + 1e-12
