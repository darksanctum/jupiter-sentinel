import json
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.autotrader as autotrader
from src.autotrader import AutoTrader
from src.config import JUP_MINT, SOL_MINT, USDC_MINT
from src.oracle import PricePoint
from src.risk import Position


class FakeFeed:
    def __init__(self, pair_name, input_mint, output_mint):
        self.pair_name = pair_name
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.history = deque(maxlen=60)


class FakeScanner:
    def __init__(self):
        self.alerts = []
        self.feeds = [FakeFeed("JUP/USDC", JUP_MINT, USDC_MINT)]
        self.stopped = False

    def scan_once(self):
        return []

    def stop(self):
        self.stopped = True


class FakeExecutor:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.trade_history = []

    def get_balance(self):
        return {
            "sol": 10.0,
            "usd_value": 1000.0,
            "sol_price": 100.0,
            "address": "fake-wallet",
        }

    def execute_swap(self, **kwargs):
        self.calls.append(kwargs)
        result = dict(self.results.pop(0))
        if result.get("status") == "success":
            self.trade_history.append(result)
        return result


class FakeRiskManager:
    def __init__(self, executor):
        self.executor = executor
        self.positions = []
        self.closed_positions = []
        self.price_feeds = {}
        self.next_actions = []

    def open_position(
        self,
        pair,
        input_mint,
        output_mint,
        amount_sol,
        stop_loss_pct=None,
        take_profit_pct=None,
        dry_run=True,
    ):
        position = Position(
            pair=pair,
            input_mint=input_mint,
            output_mint=output_mint,
            entry_price=1.25,
            amount_sol=amount_sol,
            entry_time=1000.0,
            stop_loss_pct=0.05 if stop_loss_pct is None else stop_loss_pct,
            take_profit_pct=0.15 if take_profit_pct is None else take_profit_pct,
            highest_price=1.25,
        )
        self.positions.append(position)
        self.price_feeds[pair] = FakeFeed(pair, input_mint, output_mint)
        return position

    def check_positions(self):
        actions = list(self.next_actions)
        self.next_actions = []
        for action in actions:
            for position in list(self.positions):
                if position.pair != action["pair"]:
                    continue
                position.status = "closed"
                self.positions.remove(position)
                self.closed_positions.append(
                    {
                        "position": position,
                        "action": dict(action),
                        "timestamp": "2026-04-13T00:00:00",
                    }
                )
                break
        return actions


def build_trader(tmp_path, *, executor_results=None, dry_run=True):
    scanner = FakeScanner()
    executor = FakeExecutor(results=executor_results)
    risk_manager = FakeRiskManager(executor)
    trader = AutoTrader(
        dry_run=dry_run,
        state_path=tmp_path / "state.json",
        scanner=scanner,
        executor=executor,
        risk_manager=risk_manager,
        sleep_fn=lambda _: None,
    )
    return trader, scanner, executor, risk_manager


def test_handle_alert_opens_position_and_persists_state(tmp_path):
    trader, scanner, executor, risk_manager = build_trader(
        tmp_path,
        executor_results=[
            {
                "status": "dry_run",
                "out_amount": 123456,
                "timestamp": "2026-04-13T00:00:00",
            }
        ],
    )

    trader._handle_alert(
        {
            "pair": "JUP/USDC",
            "direction": "DOWN",
            "change_pct": -4.5,
            "price": 1.11,
        }
    )

    assert len(risk_manager.positions) == 1
    assert risk_manager.positions[0].pair == "JUP/USDC"
    assert trader.position_meta["JUP/USDC"]["entry_amount_units"] == 123456
    assert executor.calls == [
        {
            "input_mint": SOL_MINT,
            "output_mint": JUP_MINT,
            "amount": 250000000,
            "dry_run": True,
        }
    ]
    assert risk_manager.price_feeds["JUP/USDC"] is scanner.feeds[0]

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["open_positions"][0]["position"]["pair"] == "JUP/USDC"
    assert state["open_positions"][0]["meta"]["held_mint"] == JUP_MINT
    assert state["open_positions"][0]["meta"]["entry_amount_units"] == 123456


def test_monitor_positions_auto_sells_and_clears_open_position(tmp_path):
    trader, _, executor, risk_manager = build_trader(
        tmp_path,
        executor_results=[
            {
                "status": "dry_run",
                "out_amount": 900000,
                "timestamp": "2026-04-13T00:00:00",
            },
            {
                "status": "dry_run",
                "out_amount": 200000000,
                "timestamp": "2026-04-13T00:01:00",
            },
        ],
    )

    trader._handle_alert(
        {
            "pair": "JUP/USDC",
            "direction": "DOWN",
            "change_pct": -5.0,
            "price": 1.02,
        }
    )

    risk_manager.next_actions = [
        {
            "type": "TAKE_PROFIT",
            "pair": "JUP/USDC",
            "pnl_pct": 12.0,
            "price": 1.20,
        }
    ]

    trader.monitor_positions()

    assert trader.position_meta == {}
    assert risk_manager.positions == []
    assert len(risk_manager.closed_positions) == 1
    assert risk_manager.closed_positions[0]["exit_result"]["status"] == "dry_run"
    assert executor.calls[1] == {
        "input_mint": JUP_MINT,
        "output_mint": SOL_MINT,
        "amount": 900000,
        "dry_run": True,
    }

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["open_positions"] == []
    assert state["closed_positions"][0]["action"]["type"] == "TAKE_PROFIT"
    assert state["closed_positions"][0]["exit_result"]["out_amount"] == 200000000


def test_monitor_positions_locks_realized_profit_after_live_profitable_exit(tmp_path, monkeypatch):
    locked_amounts = []

    def fake_lock_profit(amount):
        locked_amounts.append(amount)
        return amount * 0.5

    monkeypatch.setattr(autotrader, "lock_profit", fake_lock_profit)

    trader, _, _, risk_manager = build_trader(
        tmp_path,
        dry_run=False,
        executor_results=[
            {
                "status": "success",
                "out_amount": 900000,
                "timestamp": "2026-04-13T00:00:00",
            },
            {
                "status": "success",
                "out_amount": 300000000,
                "timestamp": "2026-04-13T00:01:00",
            },
        ],
    )

    trader._handle_alert(
        {
            "pair": "JUP/USDC",
            "direction": "DOWN",
            "change_pct": -5.0,
            "price": 1.02,
        }
    )

    risk_manager.next_actions = [
        {
            "type": "TAKE_PROFIT",
            "pair": "JUP/USDC",
            "pnl_pct": 12.0,
            "price": 1.20,
        }
    ]

    trader.monitor_positions()

    assert locked_amounts == [pytest.approx(0.05)]
    assert risk_manager.closed_positions[0]["realized_profit_sol"] == pytest.approx(0.05)
    assert risk_manager.closed_positions[0]["locked_profit_sol"] == pytest.approx(0.025)

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["closed_positions"][0]["realized_profit_sol"] == pytest.approx(0.05)
    assert state["closed_positions"][0]["locked_profit_sol"] == pytest.approx(0.025)


def test_load_state_restores_positions_feed_history_and_trade_history(tmp_path):
    state_path = tmp_path / "state.json"
    payload = {
        "version": 1,
        "updated_at": "2026-04-13T00:10:00",
        "dry_run": True,
        "cycle": 7,
        "entry_amount_sol": 0.25,
        "enter_on": "down",
        "max_open_positions": None,
        "scan_interval_secs": 30,
        "open_positions": [
            {
                "position": {
                    "pair": "JUP/USDC",
                    "input_mint": JUP_MINT,
                    "output_mint": USDC_MINT,
                    "entry_price": 1.15,
                    "amount_sol": 0.25,
                    "entry_time": 1000.0,
                    "stop_loss_pct": 0.05,
                    "take_profit_pct": 0.15,
                    "trailing_stop_pct": 0.03,
                    "highest_price": 1.22,
                    "status": "open",
                    "notional": 0.0,
                    "tx_buy": "buy-tx",
                },
                "meta": {
                    "held_mint": JUP_MINT,
                    "scan_input_mint": JUP_MINT,
                    "scan_output_mint": USDC_MINT,
                    "entry_amount_units": 999999,
                },
            }
        ],
        "closed_positions": [
            {
                "position": {
                    "pair": "JUP/USDC",
                    "input_mint": JUP_MINT,
                    "output_mint": USDC_MINT,
                    "entry_price": 1.00,
                    "amount_sol": 0.20,
                    "entry_time": 900.0,
                    "stop_loss_pct": 0.05,
                    "take_profit_pct": 0.15,
                    "trailing_stop_pct": 0.03,
                    "highest_price": 1.18,
                    "status": "closed",
                    "notional": 0.0,
                    "tx_buy": "old-buy",
                },
                "action": {
                    "type": "STOP_LOSS",
                    "pair": "JUP/USDC",
                    "pnl_pct": -5.0,
                    "price": 0.95,
                },
                "timestamp": "2026-04-13T00:05:00",
                "exit_result": {
                    "status": "dry_run",
                    "out_amount": 100000000,
                },
            }
        ],
        "trade_history": [{"status": "success", "tx_signature": "tx-123"}],
        "alerts": [{"pair": "JUP/USDC", "direction": "DOWN"}],
        "scanner_feeds": [
            {
                "pair": "JUP/USDC",
                "input_mint": JUP_MINT,
                "output_mint": USDC_MINT,
                "history": [
                    {"timestamp": 1.0, "price": 1.10, "volume_estimate": 0.0},
                    {"timestamp": 2.0, "price": 1.15, "volume_estimate": 0.0},
                ],
            }
        ],
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    scanner = FakeScanner()
    executor = FakeExecutor()
    risk_manager = FakeRiskManager(executor)

    trader = AutoTrader(
        dry_run=True,
        state_path=state_path,
        scanner=scanner,
        executor=executor,
        risk_manager=risk_manager,
        sleep_fn=lambda _: None,
    )

    assert trader.cycle == 7
    assert executor.trade_history == [{"status": "success", "tx_signature": "tx-123"}]
    assert scanner.alerts == [{"pair": "JUP/USDC", "direction": "DOWN"}]
    assert len(risk_manager.positions) == 1
    assert risk_manager.positions[0].pair == "JUP/USDC"
    assert trader.position_meta["JUP/USDC"]["entry_amount_units"] == 999999
    assert list(scanner.feeds[0].history) == [
        PricePoint(timestamp=1.0, price=1.10, volume_estimate=0.0),
        PricePoint(timestamp=2.0, price=1.15, volume_estimate=0.0),
    ]
    assert len(risk_manager.closed_positions) == 1
    assert risk_manager.closed_positions[0]["action"]["type"] == "STOP_LOSS"
