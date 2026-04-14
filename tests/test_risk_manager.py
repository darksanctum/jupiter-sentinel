import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.risk as risk
from src.oracle import PricePoint
from src.risk import Position, RiskManager


class FakeExecutor:
    def __init__(self, balance=None, swap_result=None, trade_history=None):
        self.balance = balance or {"sol": 10.0, "sol_price": 100.0, "usd_value": 1000.0}
        self.swap_result = swap_result or {"status": "success", "tx_signature": "fake-tx"}
        self.trade_history = list(trade_history or [])
        self.execute_calls = []

    def get_balance(self):
        return self.balance

    def execute_swap(self, **kwargs):
        self.execute_calls.append(kwargs)
        return self.swap_result


class FakeFeed:
    def __init__(self, price=None, current_price=None):
        self.price = price
        self.current_price = current_price if current_price is not None else price
        self.fetch_calls = 0

    def fetch_price(self):
        self.fetch_calls += 1
        if self.price is None:
            return None
        return PricePoint(timestamp=123.0, price=self.price)


def make_position(
    *,
    pair="JUP/SOL",
    entry_price=100.0,
    highest_price=None,
    stop_loss_pct=0.05,
    take_profit_pct=0.15,
    trailing_stop_pct=0.03,
    status="open",
):
    return Position(
        pair=pair,
        input_mint="input-mint",
        output_mint="output-mint",
        entry_price=entry_price,
        amount_sol=1.5,
        entry_time=1.0,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
        highest_price=entry_price if highest_price is None else highest_price,
        status=status,
    )


def test_open_position_caps_size_and_tracks_feed_in_dry_run(monkeypatch):
    executor = FakeExecutor(balance={"sol": 10.0, "sol_price": 2.0, "usd_value": 20.0})
    feed = FakeFeed(price=1.23)

    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: feed)
    monkeypatch.setattr(risk.time, "time", lambda: 111.0)

    manager = RiskManager(executor)
    position = manager.open_position(
        pair="JUP/SOL",
        input_mint="input-mint",
        output_mint="output-mint",
        amount_sol=5.0,
        dry_run=True,
    )

    assert position is not None
    assert position.amount_sol == pytest.approx(2.5)
    assert position.entry_price == pytest.approx(1.23)
    assert position.entry_time == 111.0
    assert position.highest_price == pytest.approx(1.23)
    assert position.notional == pytest.approx(5.0)
    assert position.stop_loss_pct == pytest.approx(risk.STOP_LOSS_BPS / 10000)
    assert position.take_profit_pct == pytest.approx(risk.TAKE_PROFIT_BPS / 10000)
    assert manager.positions == [position]
    assert manager.price_feeds == {"JUP/SOL": feed}
    assert executor.execute_calls == []


def test_open_position_returns_none_when_balance_is_too_small(monkeypatch):
    executor = FakeExecutor(balance={"sol": 0.0005, "sol_price": 100.0, "usd_value": 0.05})

    def should_not_build_feed(**kwargs):
        raise AssertionError("PriceFeed should not be constructed")

    monkeypatch.setattr(risk, "PriceFeed", should_not_build_feed)

    manager = RiskManager(executor)

    assert manager.open_position("JUP/SOL", "input", "output", amount_sol=1.0) is None
    assert manager.positions == []


def test_open_position_returns_none_when_entry_price_is_unavailable(monkeypatch):
    executor = FakeExecutor()
    feed = FakeFeed(price=None)
    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: feed)

    manager = RiskManager(executor)

    assert manager.open_position("JUP/SOL", "input", "output", amount_sol=1.0) is None
    assert manager.positions == []
    assert manager.price_feeds == {}


def test_open_position_caps_size_by_tradable_balance(monkeypatch):
    executor = FakeExecutor(balance={"sol": 10.0, "sol_price": 2.0, "usd_value": 20.0})
    feed = FakeFeed(price=1.23)

    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: feed)
    monkeypatch.setattr(
        risk, "get_tradable_balance", lambda total_balance, **kwargs: 0.75
    )

    manager = RiskManager(executor)
    position = manager.open_position(
        pair="JUP/SOL",
        input_mint="input-mint",
        output_mint="output-mint",
        amount_sol=5.0,
        dry_run=True,
    )

    assert position is not None
    assert position.amount_sol == pytest.approx(0.6)


def test_open_position_executes_buy_when_not_dry_run(monkeypatch):
    executor = FakeExecutor(balance={"sol": 3.0, "sol_price": 2.0, "usd_value": 6.0})
    feed = FakeFeed(price=4.2)
    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: feed)

    manager = RiskManager(executor)
    position = manager.open_position(
        pair="BONK/USDC",
        input_mint="input-mint",
        output_mint="bonk-mint",
        amount_sol=5.0,
        stop_loss_pct=0.07,
        take_profit_pct=0.2,
        dry_run=False,
    )

    assert position is not None
    assert position.amount_sol == pytest.approx(2.4)
    assert position.stop_loss_pct == pytest.approx(0.07)
    assert position.take_profit_pct == pytest.approx(0.2)
    assert position.tx_buy == "fake-tx"
    assert executor.execute_calls == [
        {
            "input_mint": risk.SOL_MINT,
            "output_mint": "bonk-mint",
            "amount": 2_400_000_000,
            "dry_run": False,
        }
    ]


def test_open_position_returns_none_when_trade_execution_fails(monkeypatch):
    executor = FakeExecutor(swap_result={"status": "failed", "error": "rejected"})
    feed = FakeFeed(price=4.2)
    monkeypatch.setattr(risk, "PriceFeed", lambda **kwargs: feed)

    manager = RiskManager(executor)

    assert manager.open_position("BONK/USDC", "input", "bonk-mint", amount_sol=1.0, dry_run=False) is None
    assert manager.positions == []
    assert manager.price_feeds == {}


def test_check_positions_closes_position_on_stop_loss():
    executor = FakeExecutor()
    manager = RiskManager(executor)
    position = make_position(entry_price=100.0)
    manager.positions = [position]
    manager.price_feeds = {position.pair: FakeFeed(price=94.0)}

    actions = manager.check_positions()

    assert actions == [
        {
            "type": "STOP_LOSS",
            "pair": "JUP/SOL",
            "pnl_pct": pytest.approx(-6.0),
            "price": 94.0,
        }
    ]
    assert manager.positions == []
    assert position.status == "closed"
    assert len(manager.closed_positions) == 1
    assert manager.closed_positions[0]["position"] is position
    assert manager.closed_positions[0]["action"]["type"] == "STOP_LOSS"


def test_check_positions_closes_position_on_take_profit():
    executor = FakeExecutor()
    manager = RiskManager(executor)
    position = make_position(entry_price=100.0)
    manager.positions = [position]
    manager.price_feeds = {position.pair: FakeFeed(price=120.0)}

    actions = manager.check_positions()

    assert actions == [
        {
            "type": "TAKE_PROFIT",
            "pair": "JUP/SOL",
            "pnl_pct": pytest.approx(20.0),
            "price": 120.0,
        }
    ]
    assert manager.positions == []
    assert position.status == "closed"


def test_check_positions_closes_position_on_trailing_stop():
    executor = FakeExecutor()
    manager = RiskManager(executor)
    position = make_position(entry_price=100.0, highest_price=110.0, take_profit_pct=0.3)
    manager.positions = [position]
    manager.price_feeds = {position.pair: FakeFeed(price=105.0)}

    actions = manager.check_positions()

    assert actions == [
        {
            "type": "TRAILING_STOP",
            "pair": "JUP/SOL",
            "pnl_pct": pytest.approx(5.0),
            "price": 105.0,
            "highest": 110.0,
        }
    ]
    assert manager.positions == []
    assert position.status == "closed"


def test_check_positions_skips_missing_data_and_updates_highest_price():
    executor = FakeExecutor()
    manager = RiskManager(executor)
    missing_feed = make_position(pair="NO/FEED")
    no_price = make_position(pair="NO/PRICE")
    rising = make_position(pair="RISING", highest_price=100.0, take_profit_pct=0.25)
    already_closed = make_position(pair="CLOSED", status="closed")

    manager.positions = [missing_feed, no_price, rising, already_closed]
    manager.price_feeds = {
        "NO/PRICE": FakeFeed(price=None),
        "RISING": FakeFeed(price=110.0),
        "CLOSED": FakeFeed(price=90.0),
    }

    actions = manager.check_positions()

    assert actions == []
    assert missing_feed in manager.positions
    assert no_price in manager.positions
    assert rising in manager.positions
    assert already_closed in manager.positions
    assert rising.highest_price == pytest.approx(110.0)
    assert manager.closed_positions == []


def test_get_portfolio_report_uses_live_feed_prices_and_trade_counts():
    executor = FakeExecutor(
        balance={"sol": 2.0, "sol_price": 150.0, "usd_value": 300.0},
        trade_history=[{"id": 1}, {"id": 2}, {"id": 3}],
    )
    manager = RiskManager(executor)
    first = make_position(pair="SOL/USDC", entry_price=100.0)
    second = make_position(pair="JUP/USDC", entry_price=50.0)
    manager.positions = [first, second]
    manager.price_feeds = {"SOL/USDC": SimpleNamespace(current_price=110.0)}
    manager.closed_positions = [{"position": first}, {"position": second}]

    report = manager.get_portfolio_report()

    assert report == {
        "wallet": {"sol": 2.0, "sol_price": 150.0, "usd_value": 300.0},
        "open_positions": [
            {
                "pair": "SOL/USDC",
                "entry": 100.0,
                "current": 110.0,
                "pnl_pct": pytest.approx(10.0),
                "amount_sol": 1.5,
                "status": "open",
            },
            {
                "pair": "JUP/USDC",
                "entry": 50.0,
                "current": 50.0,
                "pnl_pct": pytest.approx(0.0),
                "amount_sol": 1.5,
                "status": "open",
            },
        ],
        "total_trades": 3,
        "closed_positions": 2,
    }
