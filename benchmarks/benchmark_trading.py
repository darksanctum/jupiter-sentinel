#!/usr/bin/env python3
"""
Benchmark the trading path with deterministic Monte Carlo simulations.

Outputs:
- Timestamped Markdown report in benchmarks/results/
- Timestamped JSON payload in benchmarks/results/
- latest Markdown/JSON copies for easy inspection
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, TypeVar

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import STOP_LOSS_BPS, TAKE_PROFIT_BPS
from src.resilience import atomic_write_text

DEFAULT_OUTPUT_DIR = ROOT / "benchmarks" / "results"


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    trade_count: int
    seed: int
    stop_loss_pct: float
    take_profit_pct: float
    min_notional_usd: float
    max_notional_usd: float
    min_horizon_bars: int
    max_horizon_bars: int
    slippage_tolerance_bps: int


@dataclass(frozen=True, slots=True)
class PairModel:
    pair: str
    base_price: float
    liquidity_depth_usd: float
    bar_volatility: float
    route_fee_bps: float
    spread_bps: float
    average_hops: float
    weight: float


@dataclass(frozen=True, slots=True)
class RegimeModel:
    name: str
    weight: float
    drift_per_bar: float
    autoregression: float
    volatility_multiplier: float
    jump_probability: float
    jump_scale: float
    mean_reversion_strength: float
    jump_bias: float


@dataclass(frozen=True, slots=True)
class TradeSimulation:
    trade_id: int
    pair: str
    regime: str
    horizon_bars: int
    route_hops: int
    notional_usd: float
    latency_ms: float
    entry_price: float
    exit_reason: str
    exit_bar: int
    exit_price: float
    perfect_exit_bar: int
    perfect_exit_price: float
    estimated_total_slippage_bps: float
    actual_total_slippage_bps: float
    slippage_error_bps: float
    trigger_gross_pnl_usd: float
    estimated_execution_drag_usd: float
    actual_slippage_drag_usd: float
    fee_drag_usd: float
    actual_net_pnl_usd: float
    theory_pnl_usd: float
    theory_gap_usd: float
    trigger_regret_usd: float
    stop_loss_recovered_to_green: bool
    stop_loss_recovered_to_take_profit: bool
    take_profit_left_upside_pct: float
    note: str
    execution_time_ms: float


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    config: BenchmarkConfig
    trades: tuple[TradeSimulation, ...]
    total_runtime_seconds: float


PAIR_MODELS: tuple[PairModel, ...] = (
    PairModel(
        pair="SOL/USDC",
        base_price=165.0,
        liquidity_depth_usd=5_000_000.0,
        bar_volatility=0.008,
        route_fee_bps=4.0,
        spread_bps=1.2,
        average_hops=1.2,
        weight=0.18,
    ),
    PairModel(
        pair="JUP/USDC",
        base_price=1.25,
        liquidity_depth_usd=1_600_000.0,
        bar_volatility=0.013,
        route_fee_bps=7.0,
        spread_bps=2.4,
        average_hops=1.9,
        weight=0.26,
    ),
    PairModel(
        pair="JUP/SOL",
        base_price=0.0074,
        liquidity_depth_usd=900_000.0,
        bar_volatility=0.015,
        route_fee_bps=8.0,
        spread_bps=3.0,
        average_hops=2.0,
        weight=0.12,
    ),
    PairModel(
        pair="BONK/USDC",
        base_price=0.000031,
        liquidity_depth_usd=1_100_000.0,
        bar_volatility=0.019,
        route_fee_bps=11.0,
        spread_bps=4.5,
        average_hops=2.4,
        weight=0.20,
    ),
    PairModel(
        pair="WIF/USDC",
        base_price=2.85,
        liquidity_depth_usd=1_400_000.0,
        bar_volatility=0.016,
        route_fee_bps=9.0,
        spread_bps=3.4,
        average_hops=2.1,
        weight=0.24,
    ),
)


REGIME_MODELS: tuple[RegimeModel, ...] = (
    RegimeModel(
        name="trend_up",
        weight=0.20,
        drift_per_bar=0.0013,
        autoregression=0.35,
        volatility_multiplier=0.95,
        jump_probability=0.03,
        jump_scale=2.4,
        mean_reversion_strength=0.0,
        jump_bias=0.75,
    ),
    RegimeModel(
        name="trend_down",
        weight=0.18,
        drift_per_bar=-0.0015,
        autoregression=0.30,
        volatility_multiplier=1.00,
        jump_probability=0.03,
        jump_scale=2.6,
        mean_reversion_strength=0.0,
        jump_bias=-0.75,
    ),
    RegimeModel(
        name="mean_revert",
        weight=0.22,
        drift_per_bar=0.0,
        autoregression=-0.25,
        volatility_multiplier=0.85,
        jump_probability=0.02,
        jump_scale=1.8,
        mean_reversion_strength=0.18,
        jump_bias=0.0,
    ),
    RegimeModel(
        name="whipsaw",
        weight=0.22,
        drift_per_bar=0.0,
        autoregression=-0.45,
        volatility_multiplier=1.35,
        jump_probability=0.06,
        jump_scale=3.0,
        mean_reversion_strength=0.08,
        jump_bias=0.0,
    ),
    RegimeModel(
        name="breakout",
        weight=0.18,
        drift_per_bar=0.0018,
        autoregression=0.55,
        volatility_multiplier=1.40,
        jump_probability=0.08,
        jump_scale=3.2,
        mean_reversion_strength=0.02,
        jump_bias=0.55,
    ),
)


def percentile(values: Sequence[float], value: float) -> float | None:
    """Return a simple linear-interpolated percentile."""
    if not values:
        return None

    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * value
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]

    fraction = rank - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def mean(values: Sequence[float]) -> float | None:
    """Return the mean when values are present."""
    if not values:
        return None
    return sum(values) / len(values)


def rms(values: Sequence[float]) -> float | None:
    """Return root mean square when values are present."""
    if not values:
        return None
    return math.sqrt(sum(value * value for value in values) / len(values))


def format_usd(value: float | None) -> str:
    """Render a USD value for Markdown output."""
    if value is None:
        return "n/a"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def format_bps(value: float | None) -> str:
    """Render bps values for Markdown output."""
    if value is None:
        return "n/a"
    return f"{value:.2f} bps"


def format_ms(value: float | None) -> str:
    """Render ms values for Markdown output."""
    if value is None:
        return "n/a"
    return f"{value:.4f} ms"


def weighted_choice(
    items: Sequence[T],
    weights: Sequence[float],
    rng: random.Random,
) -> T:
    """Pick one item by weight from a parallel sequence."""
    if len(items) != len(weights):
        raise ValueError("items and weights must have the same length")
    total = sum(weights)
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    target = rng.random() * total
    running = 0.0
    for item, weight in zip(items, weights):
        running += weight
        if target <= running:
            return item
    return items[-1]


def log_uniform(rng: random.Random, minimum: float, maximum: float) -> float:
    """Sample a log-uniform notional."""
    if minimum <= 0 or maximum <= 0 or maximum < minimum:
        raise ValueError("log-uniform bounds must be positive and ordered")
    return math.exp(rng.uniform(math.log(minimum), math.log(maximum)))


def sample_latency_ms(rng: random.Random) -> float:
    """Sample a realistic quote-to-fill latency."""
    latency = rng.lognormvariate(math.log(320.0), 0.38)
    return min(max(latency, 60.0), 1_500.0)


def sample_route_hops(pair_model: PairModel, rng: random.Random) -> int:
    """Sample a whole-number route hop count around the pair profile."""
    raw = round(pair_model.average_hops + rng.choice((-1, 0, 0, 0, 1)))
    return max(1, min(int(raw), 4))


def generate_price_path(
    pair_model: PairModel,
    regime: RegimeModel,
    horizon_bars: int,
    rng: random.Random,
) -> list[float]:
    """Generate a future price path for one trade."""
    prices = [pair_model.base_price]
    previous_return = 0.0

    for _ in range(horizon_bars):
        sigma = pair_model.bar_volatility * regime.volatility_multiplier
        deviation = (prices[-1] / pair_model.base_price) - 1.0
        noise = rng.gauss(0.0, sigma)
        jump = 0.0

        if rng.random() < regime.jump_probability:
            sign_threshold = (regime.jump_bias + 1.0) / 2.0
            sign = 1.0 if rng.random() < sign_threshold else -1.0
            jump = sign * abs(rng.gauss(0.0, sigma * regime.jump_scale))

        step_return = (
            regime.drift_per_bar
            + (previous_return * regime.autoregression)
            - (deviation * regime.mean_reversion_strength)
            + noise
            + jump
        )
        step_return = max(step_return, -0.70)
        next_price = max(pair_model.base_price * 0.05, prices[-1] * (1.0 + step_return))
        prices.append(next_price)
        previous_return = step_return

    return prices


def evaluate_exit(
    prices: Sequence[float],
    stop_loss_pct: float,
    take_profit_pct: float,
) -> tuple[str, int, float]:
    """Mirror the current stop-loss / take-profit bar-close logic."""
    entry_price = prices[0]

    for index, price in enumerate(prices[1:], start=1):
        pnl_decimal = (price - entry_price) / entry_price
        if pnl_decimal <= -stop_loss_pct:
            return "STOP_LOSS", index, price
        if pnl_decimal >= take_profit_pct:
            return "TAKE_PROFIT", index, price

    return "TIME_EXIT", len(prices) - 1, prices[-1]


def find_perfect_exit(prices: Sequence[float]) -> tuple[int, float]:
    """Return the best possible exit bar and price with hindsight."""
    best_index = 1
    best_price = prices[1]
    for index, price in enumerate(prices[2:], start=2):
        if price > best_price:
            best_index = index
            best_price = price
    return best_index, best_price


def estimate_leg_slippage_bps(
    pair_model: PairModel,
    notional_usd: float,
    latency_ms: float,
    route_hops: int,
    regime: RegimeModel,
    urgency_multiplier: float,
) -> float:
    """Predict quote-time slippage for one leg."""
    size_ratio = max(notional_usd / pair_model.liquidity_depth_usd, 1e-9)
    depth_component = 340.0 * math.sqrt(size_ratio)
    volatility_component = (
        pair_model.bar_volatility
        * regime.volatility_multiplier
        * 10_000.0
        * 0.18
        * math.sqrt(max(latency_ms, 50.0) / 400.0)
    )
    hop_component = max(route_hops - 1, 0) * 2.2
    urgency_component = urgency_multiplier * 1.8
    return max(
        0.0,
        pair_model.spread_bps
        + depth_component
        + volatility_component
        + hop_component
        + urgency_component,
    )


def realize_leg_slippage_bps(
    pair_model: PairModel,
    notional_usd: float,
    latency_ms: float,
    route_hops: int,
    regime: RegimeModel,
    urgency_multiplier: float,
    estimated_bps: float,
    rng: random.Random,
) -> float:
    """Simulate realized slippage with model error and adverse selection."""
    size_ratio = max(notional_usd / pair_model.liquidity_depth_usd, 1e-9)
    depth_component = 420.0 * math.sqrt(size_ratio)
    volatility_component = (
        pair_model.bar_volatility
        * regime.volatility_multiplier
        * 10_000.0
        * 0.23
        * math.sqrt(max(latency_ms, 50.0) / 350.0)
    )
    hop_component = max(route_hops - 1, 0) * 2.8
    urgency_component = urgency_multiplier * 2.4
    noise = rng.gauss(0.0, max(1.5, estimated_bps * 0.18))
    adverse_selection = max(
        0.0,
        rng.gauss(
            0.0,
            3.0
            + (
                pair_model.bar_volatility
                * regime.volatility_multiplier
                * 10_000.0
                * 0.12
            ),
        ),
    )
    return max(
        0.0,
        (pair_model.spread_bps * 1.1)
        + depth_component
        + volatility_component
        + hop_component
        + urgency_component
        + noise
        + adverse_selection,
    )


def route_fee_bps(pair_model: PairModel, route_hops: int) -> float:
    """Estimate DEX route fees for the chosen path."""
    return pair_model.route_fee_bps + (max(route_hops - 1, 0) * 0.9)


def network_fee_usd(route_hops: int, latency_ms: float, rng: random.Random) -> float:
    """Estimate fixed gas / priority fees per swap leg."""
    speed_premium = max(0.0, 350.0 - min(latency_ms, 350.0)) / 350.0
    return 0.010 + (max(route_hops - 1, 0) * 0.004) + (speed_premium * 0.015) + rng.uniform(0.0, 0.008)


def execution_urgency(exit_reason: str) -> float:
    """Map trigger type to urgency for slippage simulation."""
    if exit_reason == "STOP_LOSS":
        return 1.25
    if exit_reason == "TAKE_PROFIT":
        return 0.85
    return 0.65


def build_trade_note(
    exit_reason: str,
    stop_loss_recovered_to_green: bool,
    stop_loss_recovered_to_take_profit: bool,
    take_profit_left_upside_pct: float,
    theory_gap_usd: float,
) -> str:
    """Attach an explanatory note for top-loss analysis."""
    if exit_reason == "STOP_LOSS" and stop_loss_recovered_to_take_profit:
        return "Stopped out before the path later hit the take-profit zone."
    if exit_reason == "STOP_LOSS" and stop_loss_recovered_to_green:
        return "Stopped out before a later recovery above entry."
    if exit_reason == "TAKE_PROFIT" and take_profit_left_upside_pct >= 0.10:
        return "Take-profit capped a strong trend that kept running."
    if exit_reason == "TAKE_PROFIT" and take_profit_left_upside_pct > 0:
        return "Take-profit secured gains but left upside on the table."
    if exit_reason == "TIME_EXIT" and theory_gap_usd > 0:
        return "No trigger fired, but hindsight found a better exit inside the holding window."
    return "Execution drag came mostly from fees and slippage, not the trigger itself."


def simulate_trade(
    trade_id: int,
    config: BenchmarkConfig,
    rng: random.Random,
) -> TradeSimulation:
    """Simulate one trade from entry through exit and hindsight evaluation."""
    started_at = time.perf_counter_ns()
    pair_model = weighted_choice(
        PAIR_MODELS,
        [item.weight for item in PAIR_MODELS],
        rng,
    )
    regime = weighted_choice(
        REGIME_MODELS,
        [item.weight for item in REGIME_MODELS],
        rng,
    )
    horizon_bars = rng.randint(config.min_horizon_bars, config.max_horizon_bars)
    route_hops = sample_route_hops(pair_model, rng)
    latency_ms = sample_latency_ms(rng)
    notional_usd = log_uniform(rng, config.min_notional_usd, config.max_notional_usd)
    prices = generate_price_path(pair_model, regime, horizon_bars, rng)
    exit_reason, exit_bar, exit_price = evaluate_exit(
        prices,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
    )
    perfect_exit_bar, perfect_exit_price = find_perfect_exit(prices)

    entry_price = prices[0]
    urgency = execution_urgency(exit_reason)
    estimated_entry_slippage_bps = estimate_leg_slippage_bps(
        pair_model,
        notional_usd,
        latency_ms,
        route_hops,
        regime,
        urgency_multiplier=0.55,
    )
    estimated_exit_slippage_bps = estimate_leg_slippage_bps(
        pair_model,
        notional_usd,
        latency_ms,
        route_hops,
        regime,
        urgency_multiplier=urgency,
    )
    actual_entry_slippage_bps = realize_leg_slippage_bps(
        pair_model,
        notional_usd,
        latency_ms,
        route_hops,
        regime,
        urgency_multiplier=0.55,
        estimated_bps=estimated_entry_slippage_bps,
        rng=rng,
    )
    actual_exit_slippage_bps = realize_leg_slippage_bps(
        pair_model,
        notional_usd,
        latency_ms,
        route_hops,
        regime,
        urgency_multiplier=urgency,
        estimated_bps=estimated_exit_slippage_bps,
        rng=rng,
    )

    estimated_total_slippage_bps = (
        estimated_entry_slippage_bps + estimated_exit_slippage_bps
    )
    actual_total_slippage_bps = actual_entry_slippage_bps + actual_exit_slippage_bps

    estimated_entry_fill = entry_price * (1.0 + (estimated_entry_slippage_bps / 10_000.0))
    actual_entry_fill = entry_price * (1.0 + (actual_entry_slippage_bps / 10_000.0))
    estimated_exit_fill = exit_price * (1.0 - (estimated_exit_slippage_bps / 10_000.0))
    actual_exit_fill = exit_price * (1.0 - (actual_exit_slippage_bps / 10_000.0))

    ideal_quantity = notional_usd / entry_price
    estimated_quantity = notional_usd / estimated_entry_fill
    actual_quantity = notional_usd / actual_entry_fill

    trigger_gross_pnl_usd = ideal_quantity * (exit_price - entry_price)
    estimated_executed_gross_pnl_usd = (estimated_quantity * estimated_exit_fill) - notional_usd
    actual_executed_gross_pnl_usd = (actual_quantity * actual_exit_fill) - notional_usd
    theory_pnl_usd = ideal_quantity * (perfect_exit_price - entry_price)

    total_route_fee_bps = route_fee_bps(pair_model, route_hops)
    entry_fees_usd = (notional_usd * total_route_fee_bps / 10_000.0) + network_fee_usd(
        route_hops,
        latency_ms,
        rng,
    )
    exit_notional_usd = max(actual_quantity * actual_exit_fill, 0.0)
    exit_fees_usd = (exit_notional_usd * total_route_fee_bps / 10_000.0) + network_fee_usd(
        route_hops,
        latency_ms,
        rng,
    )
    fee_drag_usd = entry_fees_usd + exit_fees_usd
    actual_net_pnl_usd = actual_executed_gross_pnl_usd - fee_drag_usd
    estimated_execution_drag_usd = trigger_gross_pnl_usd - estimated_executed_gross_pnl_usd
    actual_slippage_drag_usd = trigger_gross_pnl_usd - actual_executed_gross_pnl_usd
    trigger_regret_usd = theory_pnl_usd - trigger_gross_pnl_usd
    theory_gap_usd = theory_pnl_usd - actual_net_pnl_usd

    later_prices = list(prices[exit_bar + 1 :])
    later_peak_price = max(later_prices) if later_prices else exit_price
    stop_loss_recovered_to_green = (
        exit_reason == "STOP_LOSS" and later_peak_price > entry_price
    )
    stop_loss_recovered_to_take_profit = (
        exit_reason == "STOP_LOSS"
        and later_peak_price >= (entry_price * (1.0 + config.take_profit_pct))
    )
    take_profit_left_upside_pct = 0.0
    if exit_reason == "TAKE_PROFIT" and later_peak_price > exit_price:
        take_profit_left_upside_pct = (later_peak_price - exit_price) / exit_price

    finished_at = time.perf_counter_ns()
    note = build_trade_note(
        exit_reason=exit_reason,
        stop_loss_recovered_to_green=stop_loss_recovered_to_green,
        stop_loss_recovered_to_take_profit=stop_loss_recovered_to_take_profit,
        take_profit_left_upside_pct=take_profit_left_upside_pct,
        theory_gap_usd=theory_gap_usd,
    )
    return TradeSimulation(
        trade_id=trade_id,
        pair=pair_model.pair,
        regime=regime.name,
        horizon_bars=horizon_bars,
        route_hops=route_hops,
        notional_usd=notional_usd,
        latency_ms=latency_ms,
        entry_price=entry_price,
        exit_reason=exit_reason,
        exit_bar=exit_bar,
        exit_price=exit_price,
        perfect_exit_bar=perfect_exit_bar,
        perfect_exit_price=perfect_exit_price,
        estimated_total_slippage_bps=estimated_total_slippage_bps,
        actual_total_slippage_bps=actual_total_slippage_bps,
        slippage_error_bps=estimated_total_slippage_bps - actual_total_slippage_bps,
        trigger_gross_pnl_usd=trigger_gross_pnl_usd,
        estimated_execution_drag_usd=estimated_execution_drag_usd,
        actual_slippage_drag_usd=actual_slippage_drag_usd,
        fee_drag_usd=fee_drag_usd,
        actual_net_pnl_usd=actual_net_pnl_usd,
        theory_pnl_usd=theory_pnl_usd,
        theory_gap_usd=theory_gap_usd,
        trigger_regret_usd=trigger_regret_usd,
        stop_loss_recovered_to_green=stop_loss_recovered_to_green,
        stop_loss_recovered_to_take_profit=stop_loss_recovered_to_take_profit,
        take_profit_left_upside_pct=take_profit_left_upside_pct,
        note=note,
        execution_time_ms=(finished_at - started_at) / 1_000_000.0,
    )


def run_benchmark(config: BenchmarkConfig) -> BenchmarkRun:
    """Simulate a deterministic set of trades and capture execution timing."""
    rng = random.Random(config.seed)
    started_at = time.perf_counter()
    trades = tuple(
        simulate_trade(trade_id=index, config=config, rng=rng)
        for index in range(1, config.trade_count + 1)
    )
    finished_at = time.perf_counter()
    return BenchmarkRun(
        config=config,
        trades=trades,
        total_runtime_seconds=finished_at - started_at,
    )


def summarize_trigger_subset(trades: Sequence[TradeSimulation], exit_reason: str) -> dict[str, Any]:
    """Aggregate one exit reason versus hindsight."""
    subset = [trade for trade in trades if trade.exit_reason == exit_reason]
    if not subset:
        return {
            "count": 0,
            "actual_net_pnl_total_usd": 0.0,
            "theory_pnl_total_usd": 0.0,
            "trigger_regret_total_usd": 0.0,
            "slippage_drag_total_usd": 0.0,
            "fee_drag_total_usd": 0.0,
            "avg_theory_gap_usd": 0.0,
            "stop_loss_recovered_to_green_count": 0,
            "stop_loss_recovered_to_take_profit_count": 0,
            "avg_take_profit_left_upside_pct": 0.0,
        }

    return {
        "count": len(subset),
        "actual_net_pnl_total_usd": sum(trade.actual_net_pnl_usd for trade in subset),
        "theory_pnl_total_usd": sum(trade.theory_pnl_usd for trade in subset),
        "trigger_regret_total_usd": sum(trade.trigger_regret_usd for trade in subset),
        "slippage_drag_total_usd": sum(
            trade.actual_slippage_drag_usd for trade in subset
        ),
        "fee_drag_total_usd": sum(trade.fee_drag_usd for trade in subset),
        "avg_theory_gap_usd": mean([trade.theory_gap_usd for trade in subset]) or 0.0,
        "stop_loss_recovered_to_green_count": sum(
            trade.stop_loss_recovered_to_green for trade in subset
        ),
        "stop_loss_recovered_to_take_profit_count": sum(
            trade.stop_loss_recovered_to_take_profit for trade in subset
        ),
        "avg_take_profit_left_upside_pct": (
            mean(
                [
                    trade.take_profit_left_upside_pct * 100.0
                    for trade in subset
                    if trade.take_profit_left_upside_pct > 0
                ]
            )
            or 0.0
        ),
    }


def pair_breakdown(trades: Sequence[TradeSimulation]) -> list[dict[str, Any]]:
    """Summarize aggregate loss-vs-theory by trading pair."""
    rows = []
    for pair in sorted({trade.pair for trade in trades}):
        subset = [trade for trade in trades if trade.pair == pair]
        rows.append(
            {
                "pair": pair,
                "trade_count": len(subset),
                "actual_net_pnl_total_usd": sum(
                    trade.actual_net_pnl_usd for trade in subset
                ),
                "theory_gap_total_usd": sum(trade.theory_gap_usd for trade in subset),
                "trigger_regret_total_usd": sum(
                    trade.trigger_regret_usd for trade in subset
                ),
                "slippage_drag_total_usd": sum(
                    trade.actual_slippage_drag_usd for trade in subset
                ),
                "fee_drag_total_usd": sum(trade.fee_drag_usd for trade in subset),
            }
        )
    rows.sort(key=lambda row: row["theory_gap_total_usd"], reverse=True)
    return rows


def build_report_payload(run: BenchmarkRun) -> dict[str, Any]:
    """Assemble the benchmark payload."""
    trades = list(run.trades)
    config = asdict(run.config)
    execution_times_ms = [trade.execution_time_ms for trade in trades]
    slippage_errors_bps = [trade.slippage_error_bps for trade in trades]
    absolute_slippage_errors_bps = [abs(value) for value in slippage_errors_bps]
    actual_total_slippage_bps = [trade.actual_total_slippage_bps for trade in trades]
    estimated_total_slippage_bps = [
        trade.estimated_total_slippage_bps for trade in trades
    ]
    actual_net_pnls = [trade.actual_net_pnl_usd for trade in trades]
    theory_pnls = [trade.theory_pnl_usd for trade in trades]
    theory_gaps = [trade.theory_gap_usd for trade in trades]
    fees = [trade.fee_drag_usd for trade in trades]
    slippage_drags = [trade.actual_slippage_drag_usd for trade in trades]
    trigger_regrets = [trade.trigger_regret_usd for trade in trades]
    estimated_execution_drags = [trade.estimated_execution_drag_usd for trade in trades]

    total_gap_usd = sum(theory_gaps)
    exit_reason_counts = Counter(trade.exit_reason for trade in trades)
    worst_trades = sorted(trades, key=lambda trade: trade.theory_gap_usd, reverse=True)[:10]
    win_rate = (
        (sum(1 for value in actual_net_pnls if value > 0) / len(actual_net_pnls)) * 100.0
        if actual_net_pnls
        else 0.0
    )
    leakage = {
        "total_gap_usd": total_gap_usd,
        "trigger_regret_total_usd": sum(trigger_regrets),
        "slippage_drag_total_usd": sum(slippage_drags),
        "fee_drag_total_usd": sum(fees),
        "trigger_regret_share_pct": (
            (sum(trigger_regrets) / total_gap_usd) * 100.0 if total_gap_usd else 0.0
        ),
        "slippage_drag_share_pct": (
            (sum(slippage_drags) / total_gap_usd) * 100.0 if total_gap_usd else 0.0
        ),
        "fee_drag_share_pct": (
            (sum(fees) / total_gap_usd) * 100.0 if total_gap_usd else 0.0
        ),
    }

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "config": config,
        "execution": {
            "total_runtime_seconds": run.total_runtime_seconds,
            "avg_trade_time_ms": mean(execution_times_ms),
            "p50_trade_time_ms": percentile(execution_times_ms, 0.50),
            "p95_trade_time_ms": percentile(execution_times_ms, 0.95),
            "p99_trade_time_ms": percentile(execution_times_ms, 0.99),
            "max_trade_time_ms": max(execution_times_ms) if execution_times_ms else None,
        },
        "outcomes": {
            "trade_count": len(trades),
            "win_rate_pct": win_rate,
            "actual_net_pnl_total_usd": sum(actual_net_pnls),
            "actual_net_pnl_avg_usd": mean(actual_net_pnls),
            "theory_pnl_total_usd": sum(theory_pnls),
            "theory_pnl_avg_usd": mean(theory_pnls),
            "exit_reason_counts": dict(exit_reason_counts),
        },
        "slippage": {
            "estimated_total_slippage_bps_avg": mean(estimated_total_slippage_bps),
            "actual_total_slippage_bps_avg": mean(actual_total_slippage_bps),
            "slippage_bias_bps": mean(slippage_errors_bps),
            "slippage_mae_bps": mean(absolute_slippage_errors_bps),
            "slippage_rmse_bps": rms(slippage_errors_bps),
            "estimated_execution_drag_total_usd": sum(estimated_execution_drags),
            "actual_slippage_drag_total_usd": sum(slippage_drags),
            "slippage_estimation_error_total_usd": (
                sum(estimated_execution_drags) - sum(slippage_drags)
            ),
        },
        "fees": {
            "total_fee_cost_usd": sum(fees),
            "avg_fee_cost_usd": mean(fees),
            "p95_fee_cost_usd": percentile(fees, 0.95),
            "avg_round_trip_fee_bps": mean(
                [
                    (trade.fee_drag_usd / max(trade.notional_usd, 1e-9)) * 10_000.0
                    for trade in trades
                ]
            ),
        },
        "hindsight": {
            "stop_loss": summarize_trigger_subset(trades, "STOP_LOSS"),
            "take_profit": summarize_trigger_subset(trades, "TAKE_PROFIT"),
            "time_exit": summarize_trigger_subset(trades, "TIME_EXIT"),
        },
        "leakage": leakage,
        "pair_breakdown": pair_breakdown(trades),
        "worst_trades": [asdict(trade) for trade in worst_trades],
        "trades": [asdict(trade) for trade in trades],
    }


def render_report(payload: dict[str, Any]) -> str:
    """Render a Markdown benchmark report."""
    config = payload["config"]
    execution = payload["execution"]
    outcomes = payload["outcomes"]
    slippage = payload["slippage"]
    fees = payload["fees"]
    hindsight = payload["hindsight"]
    leakage = payload["leakage"]
    pair_rows = payload["pair_breakdown"]
    worst_trades = payload["worst_trades"]

    lines = [
        "# Jupiter Trading Benchmark Report",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Simulated trades: {outcomes['trade_count']}",
        f"- Seed: {config['seed']}",
        (
            f"- Stop-loss / take-profit: "
            f"-{config['stop_loss_pct'] * 100:.1f}% / +{config['take_profit_pct'] * 100:.1f}%"
        ),
        (
            f"- Trade notional range: "
            f"{format_usd(config['min_notional_usd'])} to {format_usd(config['max_notional_usd'])}"
        ),
        f"- Slippage tolerance modeled: {config['slippage_tolerance_bps']} bps",
        f"- Trailing stop: disabled in this benchmark to isolate stop-loss and take-profit behavior.",
        "",
        "## Execution Time",
        "",
        f"- Total runtime: {execution['total_runtime_seconds']:.4f} s",
        f"- Avg trade simulation time: {format_ms(execution['avg_trade_time_ms'])}",
        f"- p95 trade simulation time: {format_ms(execution['p95_trade_time_ms'])}",
        f"- p99 trade simulation time: {format_ms(execution['p99_trade_time_ms'])}",
        "",
        "## Outcome Summary",
        "",
        f"- Actual net PnL: {format_usd(outcomes['actual_net_pnl_total_usd'])}",
        f"- Perfect-hindsight PnL: {format_usd(outcomes['theory_pnl_total_usd'])}",
        f"- Win rate after costs: {outcomes['win_rate_pct']:.2f}%",
        (
            f"- Exit counts: STOP_LOSS={outcomes['exit_reason_counts'].get('STOP_LOSS', 0)}, "
            f"TAKE_PROFIT={outcomes['exit_reason_counts'].get('TAKE_PROFIT', 0)}, "
            f"TIME_EXIT={outcomes['exit_reason_counts'].get('TIME_EXIT', 0)}"
        ),
        "",
        "## Slippage And Fees",
        "",
        f"- Estimated total slippage: {format_bps(slippage['estimated_total_slippage_bps_avg'])} avg",
        f"- Realized total slippage: {format_bps(slippage['actual_total_slippage_bps_avg'])} avg",
        f"- Slippage MAE: {format_bps(slippage['slippage_mae_bps'])}",
        f"- Slippage bias: {format_bps(slippage['slippage_bias_bps'])}",
        f"- Estimated slippage drag: {format_usd(slippage['estimated_execution_drag_total_usd'])}",
        f"- Realized slippage drag: {format_usd(slippage['actual_slippage_drag_total_usd'])}",
        f"- Slippage estimation error: {format_usd(slippage['slippage_estimation_error_total_usd'])}",
        f"- Fee cost total: {format_usd(fees['total_fee_cost_usd'])}",
        f"- Avg fee per round trip: {format_usd(fees['avg_fee_cost_usd'])}",
        f"- Avg round-trip fee load: {format_bps(fees['avg_round_trip_fee_bps'])}",
        "",
        "## Stop-Loss Vs Take-Profit Against Hindsight",
        "",
        "| Trigger | Count | Actual Net PnL | Hindsight PnL | Trigger Regret | Slippage Drag | Fees |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| STOP_LOSS | {hindsight['stop_loss']['count']} | "
            f"{format_usd(hindsight['stop_loss']['actual_net_pnl_total_usd'])} | "
            f"{format_usd(hindsight['stop_loss']['theory_pnl_total_usd'])} | "
            f"{format_usd(hindsight['stop_loss']['trigger_regret_total_usd'])} | "
            f"{format_usd(hindsight['stop_loss']['slippage_drag_total_usd'])} | "
            f"{format_usd(hindsight['stop_loss']['fee_drag_total_usd'])} |"
        ),
        (
            f"| TAKE_PROFIT | {hindsight['take_profit']['count']} | "
            f"{format_usd(hindsight['take_profit']['actual_net_pnl_total_usd'])} | "
            f"{format_usd(hindsight['take_profit']['theory_pnl_total_usd'])} | "
            f"{format_usd(hindsight['take_profit']['trigger_regret_total_usd'])} | "
            f"{format_usd(hindsight['take_profit']['slippage_drag_total_usd'])} | "
            f"{format_usd(hindsight['take_profit']['fee_drag_total_usd'])} |"
        ),
        (
            f"| TIME_EXIT | {hindsight['time_exit']['count']} | "
            f"{format_usd(hindsight['time_exit']['actual_net_pnl_total_usd'])} | "
            f"{format_usd(hindsight['time_exit']['theory_pnl_total_usd'])} | "
            f"{format_usd(hindsight['time_exit']['trigger_regret_total_usd'])} | "
            f"{format_usd(hindsight['time_exit']['slippage_drag_total_usd'])} | "
            f"{format_usd(hindsight['time_exit']['fee_drag_total_usd'])} |"
        ),
        "",
        (
            f"- Stop-loss trades that later recovered above entry: "
            f"{hindsight['stop_loss']['stop_loss_recovered_to_green_count']}"
        ),
        (
            f"- Stop-loss trades that later would have hit take-profit: "
            f"{hindsight['stop_loss']['stop_loss_recovered_to_take_profit_count']}"
        ),
        (
            f"- Avg upside left after take-profit exits: "
            f"{hindsight['take_profit']['avg_take_profit_left_upside_pct']:.2f}%"
        ),
        "",
        "## Where Money Was Lost Vs Theory",
        "",
        f"- Total gap to perfect hindsight: {format_usd(leakage['total_gap_usd'])}",
        (
            f"- Trigger regret: {format_usd(leakage['trigger_regret_total_usd'])} "
            f"({leakage['trigger_regret_share_pct']:.2f}% of gap)"
        ),
        (
            f"- Slippage drag: {format_usd(leakage['slippage_drag_total_usd'])} "
            f"({leakage['slippage_drag_share_pct']:.2f}% of gap)"
        ),
        (
            f"- Fee drag: {format_usd(leakage['fee_drag_total_usd'])} "
            f"({leakage['fee_drag_share_pct']:.2f}% of gap)"
        ),
        "",
        "## Pair Breakdown",
        "",
        "| Pair | Trades | Actual Net PnL | Gap To Theory | Trigger Regret | Slippage | Fees |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for row in pair_rows:
        lines.append(
            f"| {row['pair']} | {row['trade_count']} | {format_usd(row['actual_net_pnl_total_usd'])} | "
            f"{format_usd(row['theory_gap_total_usd'])} | {format_usd(row['trigger_regret_total_usd'])} | "
            f"{format_usd(row['slippage_drag_total_usd'])} | {format_usd(row['fee_drag_total_usd'])} |"
        )

    lines.extend(
        [
            "",
            "## Worst Trades Vs Theory",
            "",
            "| ID | Pair | Trigger | Actual Net | Theory | Trigger Regret | Slippage | Fees | Note |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )

    for trade in worst_trades:
        lines.append(
            f"| {trade['trade_id']} | {trade['pair']} | {trade['exit_reason']} | "
            f"{format_usd(trade['actual_net_pnl_usd'])} | {format_usd(trade['theory_pnl_usd'])} | "
            f"{format_usd(trade['trigger_regret_usd'])} | {format_usd(trade['actual_slippage_drag_usd'])} | "
            f"{format_usd(trade['fee_drag_usd'])} | {trade['note']} |"
        )

    lines.extend(
        [
            "",
            "Assumptions:",
            "- This benchmark is deterministic and offline. It uses a synthetic market path model calibrated for realistic retail-sized Solana trades.",
            "- Perfect hindsight means buying at the modeled entry price and exiting at the best later bar inside the same holding window with zero fees and zero slippage.",
            "- Trigger regret, slippage drag, and fee drag are additive by construction: theory gap = trigger regret + slippage drag + fee drag.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report_artifacts(
    payload: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    """Persist timestamped and latest report artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    markdown = render_report(payload)
    json_payload = json.dumps(payload, indent=2, sort_keys=True)

    markdown_path = output_dir / f"trading_benchmark_{timestamp}.md"
    json_path = output_dir / f"trading_benchmark_{timestamp}.json"
    latest_markdown_path = output_dir / "trading_benchmark_latest.md"
    latest_json_path = output_dir / "trading_benchmark_latest.json"

    atomic_write_text(markdown_path, markdown, encoding="utf-8")
    atomic_write_text(json_path, json_payload, encoding="utf-8")
    atomic_write_text(latest_markdown_path, markdown, encoding="utf-8")
    atomic_write_text(latest_json_path, json_payload, encoding="utf-8")
    return markdown_path, json_path, latest_markdown_path, latest_json_path


def positive_int(value: str) -> int:
    """Argparse type for positive integers."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def positive_float(value: str) -> float:
    """Argparse type for positive floats."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def bounded_decimal(value: str) -> float:
    """Argparse type for percentages expressed as decimals between 0 and 1."""
    parsed = float(value)
    if parsed <= 0 or parsed >= 1:
        raise argparse.ArgumentTypeError("value must be between 0 and 1")
    return parsed


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Simulate 1000 trades, measure trigger regret versus hindsight, and "
            "quantify slippage and fee drag."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--trade-count", type=positive_int, default=1_000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--stop-loss-pct",
        type=bounded_decimal,
        default=(STOP_LOSS_BPS / 10_000.0),
    )
    parser.add_argument(
        "--take-profit-pct",
        type=bounded_decimal,
        default=(TAKE_PROFIT_BPS / 10_000.0),
    )
    parser.add_argument("--min-notional-usd", type=positive_float, default=100.0)
    parser.add_argument("--max-notional-usd", type=positive_float, default=2_500.0)
    parser.add_argument("--min-horizon-bars", type=positive_int, default=36)
    parser.add_argument("--max-horizon-bars", type=positive_int, default=144)
    parser.add_argument("--slippage-tolerance-bps", type=positive_int, default=50)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.max_notional_usd < args.min_notional_usd:
        parser.error("--max-notional-usd must be >= --min-notional-usd")
    if args.max_horizon_bars < args.min_horizon_bars:
        parser.error("--max-horizon-bars must be >= --min-horizon-bars")

    config = BenchmarkConfig(
        trade_count=args.trade_count,
        seed=args.seed,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        min_notional_usd=args.min_notional_usd,
        max_notional_usd=args.max_notional_usd,
        min_horizon_bars=args.min_horizon_bars,
        max_horizon_bars=args.max_horizon_bars,
        slippage_tolerance_bps=args.slippage_tolerance_bps,
    )
    run = run_benchmark(config)
    payload = build_report_payload(run)
    markdown_path, json_path, latest_markdown_path, latest_json_path = write_report_artifacts(
        payload,
        args.output_dir,
    )

    print(render_report(payload))
    print(f"Markdown report: {markdown_path}")
    print(f"JSON report: {json_path}")
    print(f"Latest Markdown: {latest_markdown_path}")
    print(f"Latest JSON: {latest_json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
T = TypeVar("T")

