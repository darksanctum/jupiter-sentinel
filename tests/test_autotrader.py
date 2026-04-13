import json
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.risk as risk
from src.autotrader import AutoTrader
from src.config import JUP_MINT, SOL_MINT, USDC_MINT
from src.oracle import PricePoint


class SequenceFeed:
    def __init__(self, pair_name, input_mint, output_mint, *, seed_prices=None, next_prices=None):
        self.pair_name = pair_name
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.history = deque(maxlen=60)

        for index, price in enumerate(seed_prices or []):
            self.history.append(
                PricePoint(
                    timestamp=float(index + 1),
                    price=float(price),
                    volume_estimate=0.0,
                )
            )

        self._queued_points = deque()
        for index, price in enumerate(next_prices or []):
            self._queued_points.append(
                PricePoint(
                    timestamp=float(100 + index),
                    price=float(price),
                    volume_estimate=0.0,
                )
            )

    def fetch_price(self):
        if not self._queued_points:
            return None
        point = self._queued_points.popleft()
        self.history.append(point)
        return point

    @property
    def current_price(self):
        if not self.history:
            return None
        return self.history[-1].price


class StaticEntryFeed:
    def __init__(self, pair_name, input_mint, output_mint, price):
        self.pair_name = pair_name
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.price = float(price)
        self.history = deque(maxlen=60)

    def fetch_price(self):
        point = PricePoint(timestamp=50.0, price=self.price, volume_estimate=0.0)
        self.history.append(point)
        return point


class MockScanner:
    def __init__(self, *, feeds, alert_cycles):
        self.feeds = list(feeds)
        self.alerts = []
        self._alert_cycles = deque(list(alert_cycles))
        self.stopped = False

    def scan_once(self):
        alerts = list(self._alert_cycles.popleft()) if self._alert_cycles else []
        self.alerts.extend(alerts)
        return alerts

    def stop(self):
        self.stopped = True


class FakeExecutor:
    def __init__(self, *, results, balance=None):
        self.results = list(results)
        self.balance = balance or {
            "sol": 10.0,
            "usd_value": 200.0,
            "sol_price": 20.0,
            "address": "test-wallet",
        }
        self.calls = []
        self.trade_history = []

    def get_balance(self):
        return dict(self.balance)

    def execute_swap(self, **kwargs):
        self.calls.append(dict(kwargs))
        if not self.results:
            raise AssertionError("Unexpected execute_swap call")

        result = dict(self.results.pop(0))
        if result.get("status") in {"success", "dry_run"}:
            self.trade_history.append(result)
        return result


def build_alert(price=1.0):
    return {
        "pair": "JUP/USDC",
        "direction": "DOWN",
        "change_pct": -6.0,
        "price": float(price),
    }


def patch_entry_feed(monkeypatch, price=1.0):
    monkeypatch.setattr(
        risk,
        "PriceFeed",
        lambda **kwargs: StaticEntryFeed(
            kwargs["pair_name"],
            kwargs["input_mint"],
            kwargs["output_mint"],
            price,
        ),
    )


def build_trader(tmp_path, *, scanner, executor, dry_run):
    return AutoTrader(
        dry_run=dry_run,
        state_path=tmp_path / "state.json",
        scanner=scanner,
        executor=executor,
        risk_manager=risk.RiskManager(executor),
        scan_interval_secs=1,
        sleep_fn=lambda _: None,
    )


def test_run_executes_signal_to_stop_loss_loop_and_locks_profit(tmp_path, monkeypatch):
    patch_entry_feed(monkeypatch, price=1.0)

    scanner = MockScanner(
        feeds=[
            SequenceFeed(
                "JUP/USDC",
                JUP_MINT,
                USDC_MINT,
                seed_prices=[1.02, 1.01, 1.0],
                next_prices=[0.94],
            )
        ],
        alert_cycles=[[build_alert(price=1.0)], []],
    )
    executor = FakeExecutor(
        results=[
            {
                "status": "success",
                "out_amount": 750_000,
                "out_usd": 5.0,
                "tx_signature": "buy-1",
                "timestamp": "2026-04-13T00:00:00",
            },
            {
                "status": "success",
                "out_amount": 300_000_000,
                "tx_signature": "sell-1",
                "timestamp": "2026-04-13T00:01:00",
            },
        ]
    )

    trader = build_trader(tmp_path, scanner=scanner, executor=executor, dry_run=False)
    trader.run(max_iterations=2)

    assert scanner.stopped is True
    assert trader.risk_manager.positions == []
    assert len(trader.risk_manager.closed_positions) == 1
    assert trader.risk_manager.closed_positions[0]["action"]["type"] == "STOP_LOSS"
    assert trader.risk_manager.closed_positions[0]["realized_profit_sol"] == pytest.approx(0.05)
    assert trader.risk_manager.closed_positions[0]["locked_profit_sol"] == pytest.approx(0.025)
    assert trader.position_meta == {}

    assert executor.calls == [
        {
            "input_mint": SOL_MINT,
            "output_mint": JUP_MINT,
            "amount": 250_000_000,
            "dry_run": False,
        },
        {
            "input_mint": JUP_MINT,
            "output_mint": SOL_MINT,
            "amount": 750_000,
            "dry_run": False,
        },
    ]

    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["open_positions"] == []
    assert payload["positions"]["open"] == []
    assert payload["closed_positions"][0]["action"]["type"] == "STOP_LOSS"
    assert payload["closed_positions"][0]["exit_result"]["tx_signature"] == "sell-1"
    assert payload["profit_tracking"]["realized_profit_sol"] == pytest.approx(0.05)
    assert payload["profit_tracking"]["locked_profit_sol"] == pytest.approx(0.025)
    assert payload["locked_balance"] == pytest.approx(0.025)
    assert payload["bot_config"]["cycle"] == 2
    assert payload["alerts"] == [build_alert(price=1.0)]
    assert payload["scanner_feeds"][0]["history"][-1]["price"] == pytest.approx(0.94)


def test_run_recovers_open_position_and_history_after_restart(tmp_path, monkeypatch):
    patch_entry_feed(monkeypatch, price=1.0)

    initial_alert = build_alert(price=1.0)
    state_path = tmp_path / "state.json"

    scanner_a = MockScanner(
        feeds=[
            SequenceFeed(
                "JUP/USDC",
                JUP_MINT,
                USDC_MINT,
                seed_prices=[1.03, 1.01],
            )
        ],
        alert_cycles=[[initial_alert]],
    )
    executor_a = FakeExecutor(
        results=[
            {
                "status": "dry_run",
                "out_amount": 500_000,
                "out_usd": 5.0,
                "tx_signature": "buy-1",
                "timestamp": "2026-04-13T00:00:00",
            }
        ]
    )

    trader_a = AutoTrader(
        dry_run=True,
        state_path=state_path,
        scanner=scanner_a,
        executor=executor_a,
        risk_manager=risk.RiskManager(executor_a),
        scan_interval_secs=1,
        sleep_fn=lambda _: None,
    )
    trader_a.run(max_iterations=1)

    saved_after_first_run = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved_after_first_run["open_positions"][0]["position"]["pair"] == "JUP/USDC"
    assert saved_after_first_run["trade_history"][0]["tx_signature"] == "buy-1"

    scanner_b = MockScanner(
        feeds=[
            SequenceFeed(
                "JUP/USDC",
                JUP_MINT,
                USDC_MINT,
                next_prices=[0.94],
            )
        ],
        alert_cycles=[[]],
    )
    executor_b = FakeExecutor(
        results=[
            {
                "status": "dry_run",
                "out_amount": 240_000_000,
                "tx_signature": "sell-1",
                "timestamp": "2026-04-13T00:01:00",
            }
        ]
    )

    trader_b = AutoTrader(
        dry_run=True,
        state_path=state_path,
        scanner=scanner_b,
        executor=executor_b,
        risk_manager=risk.RiskManager(executor_b),
        scan_interval_secs=1,
        sleep_fn=lambda _: None,
    )

    assert trader_b.cycle == 1
    assert len(trader_b.risk_manager.positions) == 1
    assert trader_b.risk_manager.positions[0].pair == "JUP/USDC"
    assert trader_b.position_meta["JUP/USDC"]["entry_amount_units"] == 500_000
    assert scanner_b.alerts == [initial_alert]
    assert [point.price for point in scanner_b.feeds[0].history] == pytest.approx([1.03, 1.01])
    assert [trade["tx_signature"] for trade in executor_b.trade_history] == ["buy-1"]

    trader_b.run(max_iterations=1)

    assert trader_b.risk_manager.positions == []
    assert len(trader_b.risk_manager.closed_positions) == 1
    assert trader_b.risk_manager.closed_positions[0]["action"]["type"] == "STOP_LOSS"
    assert executor_b.calls == [
        {
            "input_mint": JUP_MINT,
            "output_mint": SOL_MINT,
            "amount": 500_000,
            "dry_run": True,
        }
    ]
    assert [trade["tx_signature"] for trade in executor_b.trade_history] == ["buy-1", "sell-1"]

    recovered_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered_payload["open_positions"] == []
    assert recovered_payload["closed_positions"][0]["action"]["type"] == "STOP_LOSS"
    assert recovered_payload["trade_history"][0]["tx_signature"] == "buy-1"
    assert recovered_payload["trade_history"][1]["tx_signature"] == "sell-1"
