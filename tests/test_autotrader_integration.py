import json
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.risk as risk
from src.autotrader import AutoTrader
from src.config import JUP_MINT, SOL_MINT, USDC_MINT
from src.correlation_tracker import CorrelationTracker
from src.oracle import PriceFeed, PricePoint
from src.regime_detector import MarketRegime
from src.scanner import VolatilityScanner


class ReplayPriceFeed(PriceFeed):
    def __init__(
        self,
        *,
        pair_name: str,
        input_mint: str,
        output_mint: str,
        seed_prices: list[float] | None = None,
        next_prices: list[float] | None = None,
    ) -> None:
        super().__init__(
            pair_name=pair_name,
            input_mint=input_mint,
            output_mint=output_mint,
        )
        for index, price in enumerate(seed_prices or []):
            self.history.append(
                PricePoint(
                    timestamp=float(index + 1),
                    price=float(price),
                    volume_estimate=0.0,
                    source="mock",
                )
            )
        self._queued_points = deque(
            PricePoint(
                timestamp=float(100 + index),
                price=float(price),
                volume_estimate=0.0,
                source="mock",
            )
            for index, price in enumerate(next_prices or [])
        )

    def fetch_price(self):
        if not self._queued_points:
            return None
        point = self._queued_points.popleft()
        self.history.append(point)
        return point


class StaticEntryFeed(PriceFeed):
    def __init__(
        self,
        *,
        pair_name: str,
        input_mint: str,
        output_mint: str,
        price: float,
    ) -> None:
        super().__init__(
            pair_name=pair_name,
            input_mint=input_mint,
            output_mint=output_mint,
        )
        self._price = float(price)

    def fetch_price(self):
        point = PricePoint(
            timestamp=50.0,
            price=self._price,
            volume_estimate=0.0,
            source="mock",
        )
        self.history.append(point)
        return point


class FakeExecutor:
    def __init__(self, *, results: list[dict], balance: dict | None = None) -> None:
        self.results = list(results)
        self.balance = balance or {
            "sol": 10.0,
            "usd_value": 200.0,
            "sol_price": 20.0,
            "address": "test-wallet",
        }
        self.calls: list[dict] = []
        self.trade_history: list[dict] = []

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


def build_scanner(feed: ReplayPriceFeed) -> VolatilityScanner:
    scanner = VolatilityScanner()
    scanner.feeds = [feed]
    scanner.alerts = []
    scanner._scan_cursor = 0
    return scanner


def patch_entry_feed(monkeypatch, price: float = 1.0) -> None:
    monkeypatch.setattr(
        risk,
        "PriceFeed",
        lambda **kwargs: StaticEntryFeed(price=price, **kwargs),
    )


def build_trader(tmp_path, *, scanner, executor, state_path=None) -> AutoTrader:
    trader = AutoTrader(
        dry_run=False,
        state_path=state_path or (tmp_path / "state.json"),
        scanner=scanner,
        executor=executor,
        risk_manager=risk.RiskManager(executor),
        correlation_tracker=CorrelationTracker(path=tmp_path / "correlations.json"),
        scan_interval_secs=1,
        sleep_fn=lambda _: None,
    )
    trader.regime_detector.detect = lambda _feed: MarketRegime.SIDEWAYS
    return trader


def test_autotrader_end_to_end_signal_to_stop_loss_and_profit_lock(
    tmp_path, monkeypatch
):
    patch_entry_feed(monkeypatch, price=1.0)

    scanner = build_scanner(
        ReplayPriceFeed(
            pair_name="JUP/USDC",
            input_mint=JUP_MINT,
            output_mint=USDC_MINT,
            seed_prices=[1.05, 1.04, 1.03, 1.02, 1.01],
            next_prices=[0.94, 0.94],
        )
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

    trader = build_trader(tmp_path, scanner=scanner, executor=executor)
    trader.run(max_iterations=2)

    assert scanner.alerts
    assert scanner.alerts[0]["pair"] == "JUP/USDC"
    assert scanner.alerts[0]["direction"] == "DOWN"
    assert scanner.alerts[0]["change_pct"] == pytest.approx(-10.476190476190478)
    assert scanner.alerts[0]["severity"] == "HIGH"

    assert trader.risk_manager.positions == []
    assert len(trader.risk_manager.closed_positions) == 1
    closed_record = trader.risk_manager.closed_positions[0]
    assert closed_record["action"]["type"] == "STOP_LOSS"
    assert closed_record["exit_result"]["tx_signature"] == "sell-1"
    assert closed_record["realized_profit_sol"] == pytest.approx(0.05)
    assert closed_record["locked_profit_sol"] == pytest.approx(0.025)
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
    assert payload["closed_positions"][0]["action"]["type"] == "STOP_LOSS"
    assert payload["closed_positions"][0]["locked_profit_sol"] == pytest.approx(0.025)
    assert payload["profit_tracking"]["realized_profit_sol"] == pytest.approx(0.05)
    assert payload["profit_tracking"]["locked_profit_sol"] == pytest.approx(0.025)
    assert payload["locked_balance"] == pytest.approx(0.025)
    assert payload["trade_history"][0]["tx_signature"] == "buy-1"
    assert payload["trade_history"][1]["tx_signature"] == "sell-1"
    assert payload["alerts"][0]["pair"] == "JUP/USDC"
    assert payload["scanner_feeds"][0]["history"][-1]["price"] == pytest.approx(0.94)


def test_autotrader_recovers_open_position_after_restart_and_closes_it(
    tmp_path, monkeypatch
):
    patch_entry_feed(monkeypatch, price=1.0)

    state_path = tmp_path / "state.json"
    scanner_a = build_scanner(
        ReplayPriceFeed(
            pair_name="JUP/USDC",
            input_mint=JUP_MINT,
            output_mint=USDC_MINT,
            seed_prices=[1.05, 1.04, 1.03, 1.02, 1.01],
            next_prices=[0.94],
        )
    )
    executor_a = FakeExecutor(
        results=[
            {
                "status": "success",
                "out_amount": 750_000,
                "out_usd": 5.0,
                "tx_signature": "buy-1",
                "timestamp": "2026-04-13T00:00:00",
            }
        ]
    )

    trader_a = build_trader(
        tmp_path,
        scanner=scanner_a,
        executor=executor_a,
        state_path=state_path,
    )
    trader_a.run(max_iterations=1)

    saved_after_crash = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved_after_crash["open_positions"][0]["position"]["pair"] == "JUP/USDC"
    assert saved_after_crash["trade_history"][0]["tx_signature"] == "buy-1"
    assert saved_after_crash["closed_positions"] == []

    scanner_b = build_scanner(
        ReplayPriceFeed(
            pair_name="JUP/USDC",
            input_mint=JUP_MINT,
            output_mint=USDC_MINT,
            next_prices=[0.94],
        )
    )
    executor_b = FakeExecutor(
        results=[
            {
                "status": "success",
                "out_amount": 300_000_000,
                "tx_signature": "sell-1",
                "timestamp": "2026-04-13T00:01:00",
            }
        ]
    )

    trader_b = build_trader(
        tmp_path,
        scanner=scanner_b,
        executor=executor_b,
        state_path=state_path,
    )

    assert trader_b.cycle == 1
    assert len(trader_b.risk_manager.positions) == 1
    assert trader_b.risk_manager.positions[0].pair == "JUP/USDC"
    assert trader_b.position_meta["JUP/USDC"]["entry_amount_units"] == 750_000
    assert scanner_b.alerts[0]["pair"] == "JUP/USDC"
    assert [point.price for point in scanner_b.feeds[0].history] == pytest.approx(
        [1.05, 1.04, 1.03, 1.02, 1.01, 0.94]
    )
    assert [trade["tx_signature"] for trade in executor_b.trade_history] == ["buy-1"]

    trader_b.run(max_iterations=1)

    assert trader_b.risk_manager.positions == []
    assert len(trader_b.risk_manager.closed_positions) == 1
    closed_record = trader_b.risk_manager.closed_positions[0]
    assert closed_record["action"]["type"] == "STOP_LOSS"
    assert closed_record["locked_profit_sol"] == pytest.approx(0.025)
    assert executor_b.calls == [
        {
            "input_mint": JUP_MINT,
            "output_mint": SOL_MINT,
            "amount": 750_000,
            "dry_run": False,
        }
    ]
    assert [trade["tx_signature"] for trade in executor_b.trade_history] == [
        "buy-1",
        "sell-1",
    ]

    recovered_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered_payload["open_positions"] == []
    assert recovered_payload["closed_positions"][0]["action"]["type"] == "STOP_LOSS"
    assert recovered_payload["closed_positions"][0]["locked_profit_sol"] == pytest.approx(
        0.025
    )
    assert recovered_payload["profit_tracking"]["locked_profit_sol"] == pytest.approx(
        0.025
    )
    assert recovered_payload["trade_history"][0]["tx_signature"] == "buy-1"
    assert recovered_payload["trade_history"][1]["tx_signature"] == "sell-1"
