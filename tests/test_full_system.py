import json
import math
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.risk as risk_module
from src.autotrader import AutoTrader
from src.config import BONK_MINT, JUP_MINT, MAX_POSITION_USD, SOL_MINT, USDC_MINT, WIF_MINT
from src.correlation_tracker import CorrelationTracker
from src.oracle import PriceFeed, PricePoint
from src.regime_detector import MarketRegime
from src.strategies.arbitrage import DEFAULT_TRIANGLES, KNOWN_TOKEN_DECIMALS, TriangleQuote, TriangularArbitrageScanner
from src.strategies.mean_reversion import scan_for_signals as scan_mean_reversion_signals
from src.strategies.momentum import scan_for_signals as scan_momentum_signals
from src.strategies.smart_dca import simulate_smart_dca


PAIR_CONFIGS = (
    (SOL_MINT, USDC_MINT, "SOL/USDC", 20.0),
    (JUP_MINT, USDC_MINT, "JUP/USDC", 1.0),
    (BONK_MINT, USDC_MINT, "BONK/USDC", 0.00003),
    (WIF_MINT, USDC_MINT, "WIF/USDC", 2.0),
)

TOKEN_DECIMALS = {
    SOL_MINT: 9,
    USDC_MINT: 6,
    JUP_MINT: 6,
    BONK_MINT: 6,
    WIF_MINT: 6,
}

MINT_TO_MARKET_PAIR = {
    SOL_MINT: "SOL/USDC",
    JUP_MINT: "JUP/USDC",
    BONK_MINT: "BONK/USDC",
    WIF_MINT: "WIF/USDC",
}


@dataclass
class WalletState:
    sol_balance: float = 10.0
    token_balances: dict[str, int] = field(default_factory=dict)
    next_tx_id: int = 1


@dataclass
class PositionPlan:
    outcome: str
    hold_cycles: int


@dataclass
class PairState:
    input_mint: str
    output_mint: str
    pair_name: str
    base_price: float
    price: float
    seed_phase: int


class CycleFeed(PriceFeed):
    def __init__(
        self,
        *,
        market: "SimulatedMarket",
        pair_name: str,
        input_mint: str,
        output_mint: str,
        seed_prices: list[float],
    ) -> None:
        super().__init__(
            pair_name=pair_name,
            input_mint=input_mint,
            output_mint=output_mint,
        )
        self.market = market
        for index, price in enumerate(seed_prices):
            self.history.append(
                PricePoint(
                    timestamp=float(index + 1),
                    price=float(price),
                    volume_estimate=0.0,
                    source="mock",
                )
            )

    def fetch_price(self):
        return self.market.emit_point(self)


class EntryPriceFeed(PriceFeed):
    def __init__(
        self,
        *,
        market: "SimulatedMarket",
        pair_name: str,
        input_mint: str,
        output_mint: str,
    ) -> None:
        super().__init__(
            pair_name=pair_name,
            input_mint=input_mint,
            output_mint=output_mint,
        )
        self.market = market

    def fetch_price(self):
        point = PricePoint(
            timestamp=float(max(self.market.current_cycle, 1)),
            price=self.market.current_pair_price(self.pair_name),
            volume_estimate=0.0,
            source="mock",
        )
        self.history.append(point)
        return point


class FullScanSimulationScanner:
    def __init__(self, feeds: list[CycleFeed]) -> None:
        self.feeds = list(feeds)
        self.alerts: list[dict[str, object]] = []
        self.running = False

    def scan_once(self) -> list[dict[str, object]]:
        new_alerts: list[dict[str, object]] = []
        timestamp = f"cycle-{len(self.alerts) + 1}"

        for feed in self.feeds:
            point = feed.fetch_price()
            if point is None:
                continue

            change = feed.price_change_pct
            if abs(change) > 0.03 and len(feed.history) >= 5:
                alert = {
                    "timestamp": timestamp,
                    "pair": feed.pair_name,
                    "price": point.price,
                    "change_pct": change * 100.0,
                    "volatility": feed.volatility,
                    "direction": "UP" if change > 0 else "DOWN",
                    "severity": "HIGH" if abs(change) > 0.10 else "MEDIUM",
                }
                new_alerts.append(alert)
                self.alerts.append(alert)

        return new_alerts

    def stop(self) -> None:
        self.running = False


class SimulatedMarket:
    def __init__(self, *, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.current_cycle = 0
        self.states = {
            pair_name: PairState(
                input_mint=input_mint,
                output_mint=output_mint,
                pair_name=pair_name,
                base_price=base_price,
                price=base_price,
                seed_phase=index + 1,
            )
            for index, (input_mint, output_mint, pair_name, base_price) in enumerate(
                PAIR_CONFIGS
            )
        }
        self.pending_points: dict[str, PricePoint] = {}
        self.last_emitted_cycle: dict[str, int] = {}
        self.position_plans: dict[str, PositionPlan] = {}

    def build_feeds(self) -> list[CycleFeed]:
        return [
            CycleFeed(
                market=self,
                pair_name=state.pair_name,
                input_mint=state.input_mint,
                output_mint=state.output_mint,
                seed_prices=self._seed_prices(state),
            )
            for state in self.states.values()
        ]

    def _seed_prices(self, state: PairState) -> list[float]:
        prices: list[float] = []
        for offset in range(30):
            wobble = (
                math.sin((offset + state.seed_phase) / 3.0) * 0.0045
                + math.cos((offset + state.seed_phase * 2) / 5.0) * 0.0025
            )
            prices.append(state.base_price * (1.0 + wobble))
        state.price = prices[-1]
        return prices

    def emit_point(self, feed: CycleFeed) -> PricePoint:
        point = self.pending_points.get(feed.pair_name)
        if point is None:
            point = PricePoint(
                timestamp=float(self.current_cycle),
                price=self.current_pair_price(feed.pair_name),
                volume_estimate=0.0,
                source="mock",
            )
            self.pending_points[feed.pair_name] = point

        if self.last_emitted_cycle.get(feed.pair_name) != self.current_cycle:
            feed.history.append(point)
            self.last_emitted_cycle[feed.pair_name] = self.current_cycle
        return point

    def current_pair_price(self, pair_name: str) -> float:
        return self.states[pair_name].price

    def current_price_for_mint(self, mint: str) -> float:
        pair_name = MINT_TO_MARKET_PAIR[mint]
        return self.current_pair_price(pair_name)

    def advance(self, trader: AutoTrader) -> None:
        self.current_cycle += 1
        open_positions = {
            position.pair: position
            for position in trader.risk_manager.positions
            if position.status == "open"
        }

        for pair_name, state in self.states.items():
            position = open_positions.get(pair_name)
            if position is None:
                next_price = self._advance_watch_price(state)
            else:
                next_price = self._advance_open_position_price(state, position)

            state.price = next_price
            self.pending_points[pair_name] = PricePoint(
                timestamp=float(self.current_cycle),
                price=next_price,
                volume_estimate=0.0,
                source="mock",
            )

    def _advance_watch_price(self, state: PairState) -> float:
        mean_reversion = ((state.base_price - state.price) / state.base_price) * 0.18
        shock = self.rng.uniform(-0.08, 0.08)
        if (self.current_cycle + state.seed_phase) % 7 == 0:
            shock += self.rng.choice((-0.05, 0.05))
        next_price = state.price * (1.0 + mean_reversion + shock)
        return max(state.base_price * 0.25, next_price)

    def _advance_open_position_price(self, state: PairState, position) -> float:
        plan = self.position_plans.get(state.pair_name)
        if plan is None:
            return state.price

        stop_price = position.entry_price * (1.0 - position.stop_loss_pct)
        take_profit_price = position.entry_price * (1.0 + position.take_profit_pct)

        if plan.hold_cycles > 0:
            plan.hold_cycles -= 1
            hold_move = state.price * (1.0 + self.rng.uniform(-0.015, 0.015))
            floor_price = position.entry_price * (1.0 - (position.stop_loss_pct * 0.65))
            ceiling_price = position.entry_price * (
                1.0 + (position.take_profit_pct * 0.65)
            )
            return min(max(hold_move, floor_price), ceiling_price)

        if plan.outcome == "stop":
            candidate = state.price * (1.0 - self.rng.uniform(0.02, 0.04))
            return stop_price if candidate <= stop_price else candidate

        candidate = state.price * (1.0 + self.rng.uniform(0.03, 0.08))
        return take_profit_price if candidate >= take_profit_price else candidate

    def sync_position_plans(self, trader: AutoTrader) -> None:
        open_pairs = {
            position.pair
            for position in trader.risk_manager.positions
            if position.status == "open"
        }

        for pair_name in list(self.position_plans):
            if pair_name not in open_pairs:
                self.position_plans.pop(pair_name, None)

        for position in trader.risk_manager.positions:
            if position.status != "open" or position.pair in self.position_plans:
                continue

            self.position_plans[position.pair] = PositionPlan(
                outcome="profit" if self.rng.random() >= 0.45 else "stop",
                hold_cycles=6 + self.rng.randint(0, 8),
            )


class SimulatedExecutor:
    def __init__(self, *, market: SimulatedMarket, wallet: WalletState) -> None:
        self.market = market
        self.wallet = wallet
        self.trade_history: list[dict[str, object]] = []
        self.calls: list[dict[str, object]] = []

    def get_balance(self):
        sol_price = self.market.current_price_for_mint(SOL_MINT)
        return {
            "sol": self.wallet.sol_balance,
            "usd_value": self.wallet.sol_balance * sol_price,
            "sol_price": sol_price,
            "address": "simulated-wallet",
        }

    def execute_swap(self, **kwargs):
        self.calls.append(dict(kwargs))

        input_mint = str(kwargs["input_mint"])
        output_mint = str(kwargs["output_mint"])
        amount = int(kwargs["amount"])

        result: dict[str, object] = {
            "timestamp": f"cycle-{self.market.current_cycle}",
            "input_mint": input_mint,
            "output_mint": output_mint,
            "amount": amount,
            "status": "success",
            "tx_signature": f"sim-tx-{self.wallet.next_tx_id}",
        }
        self.wallet.next_tx_id += 1

        if input_mint == SOL_MINT:
            sol_amount = amount / 1e9
            assert self.wallet.sol_balance + 1e-12 >= sol_amount
            output_price = self.market.current_price_for_mint(output_mint)
            sol_price = self.market.current_price_for_mint(SOL_MINT)
            output_decimals = TOKEN_DECIMALS[output_mint]
            output_units = int((sol_amount * sol_price / output_price) * (10**output_decimals))
            self.wallet.sol_balance -= sol_amount
            self.wallet.token_balances[output_mint] = (
                self.wallet.token_balances.get(output_mint, 0) + output_units
            )
            result["out_amount"] = output_units
            result["out_usd"] = sol_amount * sol_price
        else:
            input_decimals = TOKEN_DECIMALS[input_mint]
            sol_price = self.market.current_price_for_mint(SOL_MINT)
            input_price = self.market.current_price_for_mint(input_mint)
            output_lamports = int((amount / (10**input_decimals) * input_price / sol_price) * 1e9)
            self.wallet.token_balances[input_mint] = max(
                self.wallet.token_balances.get(input_mint, 0) - amount,
                0,
            )
            self.wallet.sol_balance += output_lamports / 1e9
            result["out_amount"] = output_lamports
            result["out_usd"] = (output_lamports / 1e9) * sol_price

        self.trade_history.append(result)
        return result


def build_trader(
    *,
    market: SimulatedMarket,
    wallet: WalletState,
    state_path: Path,
    correlation_path: Path,
) -> tuple[AutoTrader, SimulatedExecutor]:
    scanner = FullScanSimulationScanner(market.build_feeds())
    executor = SimulatedExecutor(market=market, wallet=wallet)
    correlation_tracker = CorrelationTracker(
        path=correlation_path,
        threshold=1.0,
        refresh_interval_seconds=1.0,
        time_fn=lambda: float(market.current_cycle),
    )

    trader = AutoTrader(
        dry_run=False,
        entry_amount_sol=0.25,
        enter_on="all",
        max_open_positions=2,
        scan_interval_secs=1,
        state_path=state_path,
        scanner=scanner,
        executor=executor,
        risk_manager=risk_module.RiskManager(executor),
        correlation_tracker=correlation_tracker,
        sleep_fn=lambda _: None,
    )
    trader.regime_detector.detect = lambda _feed: MarketRegime.SIDEWAYS
    return trader, executor


def run_one_cycle(trader: AutoTrader, market: SimulatedMarket) -> None:
    market.advance(trader)
    trader._run_cycle()
    market.sync_position_plans(trader)


def assert_risk_limits(trader: AutoTrader) -> None:
    open_positions = [
        position
        for position in trader.risk_manager.positions
        if position.status == "open"
    ]
    assert len(open_positions) <= int(trader.max_open_positions or 0)
    assert sum(position.notional for position in open_positions) <= (
        MAX_POSITION_USD * len(open_positions)
    ) + 1e-9
    for position in open_positions:
        assert position.notional <= MAX_POSITION_USD + 1e-9
        assert position.amount_sol > 0
        assert position.entry_price > 0


def make_strategy_feed(
    pair_name: str,
    prices: list[float],
    *,
    input_mint: str = JUP_MINT,
    output_mint: str = USDC_MINT,
) -> PriceFeed:
    feed = PriceFeed(pair_name=pair_name, input_mint=input_mint, output_mint=output_mint)
    for index, price in enumerate(prices):
        feed.history.append(
            PricePoint(
                timestamp=float(1_700_000_000 + index),
                price=float(price),
                volume_estimate=0.0,
                source="mock",
            )
        )
    return feed


def make_triangle_quote(
    input_mint: str,
    output_mint: str,
    input_amount: int,
    out_amount: int,
    *,
    route_labels: tuple[str, ...],
    platform_fee_amount: int = 0,
    platform_fee_mint: str = "",
) -> TriangleQuote:
    return TriangleQuote(
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount=input_amount,
        out_amount=out_amount,
        input_decimals=KNOWN_TOKEN_DECIMALS[input_mint],
        output_decimals=KNOWN_TOKEN_DECIMALS[output_mint],
        route_labels=route_labels,
        price_impact_pct=0.0,
        platform_fee_amount=platform_fee_amount,
        platform_fee_mint=platform_fee_mint,
        platform_fee_decimals=KNOWN_TOKEN_DECIMALS.get(platform_fee_mint or output_mint),
    )


def assert_valid_signal(signal: dict[str, object], *, required_keys: tuple[str, ...], numeric_keys: tuple[str, ...]) -> None:
    for key in required_keys:
        assert key in signal
        assert signal[key] not in (None, "")
    for key in numeric_keys:
        assert math.isfinite(float(signal[key]))


def test_full_system_survives_restart_and_enforces_risk(monkeypatch, tmp_path):
    market = SimulatedMarket(seed=42)
    wallet = WalletState()
    state_path = tmp_path / "state.json"
    correlation_path = tmp_path / "correlations.json"

    monkeypatch.setattr(
        risk_module,
        "PriceFeed",
        lambda **kwargs: EntryPriceFeed(market=market, **kwargs),
    )

    trader, executor = build_trader(
        market=market,
        wallet=wallet,
        state_path=state_path,
        correlation_path=correlation_path,
    )
    trader._reset_runtime_state()

    restart_cycle = None
    restart_open_pairs: set[str] = set()
    pre_restart_trade_count = 0
    pre_restart_alert_count = 0

    for _ in range(100):
        run_one_cycle(trader, market)
        assert_risk_limits(trader)

        stop_loss_records = [
            record
            for record in trader.risk_manager.closed_positions
            if record["action"]["type"] == "STOP_LOSS"
        ]
        for record in stop_loss_records:
            stop_loss_pct = float(record["position"].stop_loss_pct) * 100.0
            assert float(record["action"]["pnl_pct"]) >= (-stop_loss_pct - 1e-9)

        if restart_cycle is None and trader.cycle >= 40 and trader.risk_manager.positions:
            restart_cycle = trader.cycle
            restart_open_pairs = {position.pair for position in trader.risk_manager.positions}
            pre_restart_trade_count = len(executor.trade_history)
            pre_restart_alert_count = len(trader.scanner.alerts)
            trader.stop()

            saved_payload = json.loads(state_path.read_text(encoding="utf-8"))
            assert saved_payload["bot_config"]["cycle"] == restart_cycle
            assert {record["position"]["pair"] for record in saved_payload["open_positions"]} == restart_open_pairs
            assert len(saved_payload["trade_history"]) == pre_restart_trade_count
            assert len(saved_payload["alerts"]) == pre_restart_alert_count

            trader, executor = build_trader(
                market=market,
                wallet=wallet,
                state_path=state_path,
                correlation_path=correlation_path,
            )
            trader._reset_runtime_state()

            assert trader.cycle == restart_cycle
            assert {position.pair for position in trader.risk_manager.positions} == restart_open_pairs
            assert len(executor.trade_history) == pre_restart_trade_count
            assert len(trader.scanner.alerts) == pre_restart_alert_count
            assert trader.position_meta.keys() == restart_open_pairs
            market.sync_position_plans(trader)

    trader.stop()

    assert restart_cycle is not None
    assert trader.cycle == 100
    assert trader.scanner.alerts
    assert any(record["action"]["type"] == "STOP_LOSS" for record in trader.risk_manager.closed_positions)
    assert any(record.get("locked_profit_sol", 0.0) > 0 for record in trader.risk_manager.closed_positions)

    final_payload = json.loads(state_path.read_text(encoding="utf-8"))
    closed_payload = final_payload["closed_positions"]
    total_locked = sum(float(record.get("locked_profit_sol", 0.0) or 0.0) for record in closed_payload)
    total_realized = sum(float(record.get("realized_profit_sol", 0.0) or 0.0) for record in closed_payload)
    expected_locked = total_realized * 0.5

    assert final_payload["bot_config"]["cycle"] == 100
    assert final_payload["profit_tracking"]["realized_profit_sol"] == pytest.approx(total_realized)
    assert final_payload["profit_tracking"]["locked_profit_sol"] == pytest.approx(total_locked)
    assert final_payload["locked_balance"] == pytest.approx(total_locked)
    assert total_locked == pytest.approx(expected_locked)
    assert total_locked > 0

    for record in closed_payload:
        action = record["action"]["type"]
        if action == "STOP_LOSS":
            stop_loss_pct = float(record["position"]["stop_loss_pct"]) * 100.0
            assert float(record["action"]["pnl_pct"]) >= -stop_loss_pct - 1e-9
        if float(record.get("realized_profit_sol", 0.0) or 0.0) > 0:
            assert float(record["locked_profit_sol"]) == pytest.approx(
                float(record["realized_profit_sol"]) * 0.5
            )

    mean_reversion_signal = scan_mean_reversion_signals(
        [make_strategy_feed("MR/USDC", [100.0] * 19 + [90.0])]
    )[0]
    momentum_signal = scan_momentum_signals(
        [make_strategy_feed("MOMO/USDC", [100.0, 101.0, 102.5, 104.5])]
    )[0]
    smart_dca_state = simulate_smart_dca(
        make_strategy_feed("DCA/USDC", [100.0] * 19 + [90.0]),
        base_amount=1.0,
        multiplier=2.0,
    )

    assert_valid_signal(
        mean_reversion_signal,
        required_keys=("strategy", "pair", "action", "side", "reason", "target_price"),
        numeric_keys=("price", "moving_average", "lower_band", "upper_band", "z_score", "deviation_pct"),
    )
    assert_valid_signal(
        momentum_signal,
        required_keys=("strategy", "pair", "action", "side", "reason", "entry_mode"),
        numeric_keys=("price", "momentum_score", "average_increase_pct", "cumulative_change_pct", "allocation_fraction"),
    )

    assert smart_dca_state.entries
    assert smart_dca_state.entries[-1].reason in {
        "insufficient_history",
        "base_dca",
        "price_below_lower_band",
        "price_above_upper_band",
    }
    assert math.isfinite(smart_dca_state.average_entry_price)
    assert math.isfinite(smart_dca_state.entries[-1].allocation_multiplier)

    starting_amount = 1_000_000_000
    arbitrage_scanner = TriangularArbitrageScanner(
        min_net_profit_pct=0.5,
        gas_cost_lamports_per_swap=5_000,
    )
    quote_map = {
        (SOL_MINT, USDC_MINT, starting_amount): make_triangle_quote(
            SOL_MINT,
            USDC_MINT,
            starting_amount,
            150_000_000,
            route_labels=("Meteora",),
            platform_fee_amount=200_000,
            platform_fee_mint=USDC_MINT,
        ),
        (USDC_MINT, JUP_MINT, 150_000_000): make_triangle_quote(
            USDC_MINT,
            JUP_MINT,
            150_000_000,
            300_000_000,
            route_labels=("Orca",),
        ),
        (JUP_MINT, SOL_MINT, 300_000_000): make_triangle_quote(
            JUP_MINT,
            SOL_MINT,
            300_000_000,
            1_008_000_000,
            route_labels=("Raydium",),
        ),
    }
    arbitrage_scanner.get_quote = lambda input_mint, output_mint, amount: quote_map.get(
        (input_mint, output_mint, amount)
    )
    arbitrage_evaluation = arbitrage_scanner.scan_triangle(
        DEFAULT_TRIANGLES[0],
        starting_amount=starting_amount,
    )

    assert arbitrage_evaluation is not None
    assert arbitrage_evaluation.is_opportunity is True
    assert math.isfinite(arbitrage_evaluation.net_profit_pct)
    assert arbitrage_evaluation.net_profit_amount > 0
    assert [leg.route_signature for leg in arbitrage_evaluation.legs] == [
        "Meteora",
        "Orca",
        "Raydium",
    ]
