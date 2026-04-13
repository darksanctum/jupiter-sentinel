"""
Momentum strategy based on trailing consecutive price increases.

Signals are emitted for feeds whose latest price action shows at least three
consecutive upward moves above a configurable minimum percentage threshold.
Entries are expressed as staged scale-in plans rather than a single full-size
position.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, List, Sequence

from ..oracle import PricePoint

DEFAULT_MIN_CONSECUTIVE_INCREASES = 3
DEFAULT_MIN_STEP_CHANGE_PCT = 1.0
DEFAULT_SCALE_STAGES = 3
THRESHOLD_EPSILON = 1e-9


def _normalize_prices(values: Iterable[Any]) -> list[float]:
    prices: list[float] = []
    for value in values:
        price = float(getattr(value, "price", value))
        if not math.isfinite(price) or price <= 0:
            raise ValueError("prices must contain only positive finite values")
        prices.append(price)
    return prices


def _step_change_pct(previous: float, current: float) -> float:
    if previous <= 0:
        raise ValueError("prices must contain only positive finite values")
    return ((current - previous) / previous) * 100


def _trailing_changes(prices: Sequence[float], *, min_step_change_pct: float) -> list[float]:
    trailing_changes: list[float] = []

    for index in range(len(prices) - 1, 0, -1):
        change_pct = _step_change_pct(prices[index - 1], prices[index])
        if prices[index] > prices[index - 1] and (change_pct + THRESHOLD_EPSILON) >= min_step_change_pct:
            trailing_changes.append(change_pct)
            continue
        break

    trailing_changes.reverse()
    return trailing_changes


def _momentum_stats(prices: Sequence[float], *, min_step_change_pct: float) -> dict[str, float]:
    trailing_changes = _trailing_changes(prices, min_step_change_pct=min_step_change_pct)
    if not trailing_changes:
        return {
            "score": 0.0,
            "streak_count": 0.0,
            "latest_change_pct": 0.0,
            "average_step_change_pct": 0.0,
            "cumulative_change_pct": 0.0,
        }

    streak_count = len(trailing_changes)
    streak_start_price = prices[-streak_count - 1]
    cumulative_change_pct = _step_change_pct(streak_start_price, prices[-1])
    average_step_change_pct = sum(trailing_changes) / streak_count
    score = min(
        100.0,
        (streak_count * 15.0) + (cumulative_change_pct * 6.0) + (average_step_change_pct * 8.0),
    )

    return {
        "score": score,
        "streak_count": float(streak_count),
        "latest_change_pct": trailing_changes[-1],
        "average_step_change_pct": average_step_change_pct,
        "cumulative_change_pct": cumulative_change_pct,
    }


def _target_position_fraction(score: float) -> float:
    if score >= 90.0:
        return 1.0
    if score >= 80.0:
        return 0.75
    if score >= 70.0:
        return 0.6
    return 0.4


def _scale_plan(
    *,
    target_position_fraction: float,
    scale_stages: int,
    min_step_change_pct: float,
) -> list[dict[str, Any]]:
    weights = list(range(scale_stages, 0, -1))
    total_weight = sum(weights)
    plan: list[dict[str, Any]] = []

    for stage, weight in enumerate(weights, start=1):
        fraction_of_target = weight / total_weight
        fraction_of_max = target_position_fraction * fraction_of_target
        if stage == 1:
            trigger = "enter_on_current_signal"
        elif stage == 2:
            trigger = f"add_if_next_tick_gains_at_least_{min_step_change_pct:.2f}%"
        else:
            trigger = (
                f"add_if_{stage - 1}_more_consecutive_ticks_gain_"
                f"at_least_{min_step_change_pct:.2f}%"
            )

        plan.append(
            {
                "stage": stage,
                "fraction_of_target_position": fraction_of_target,
                "fraction_of_max_position": fraction_of_max,
                "trigger": trigger,
            }
        )

    return plan


def momentum_score(
    price_points: Iterable[Any],
    *,
    min_step_change_pct: float = DEFAULT_MIN_STEP_CHANGE_PCT,
) -> float:
    """
    Score the latest trailing momentum on a 0-100 scale.

    The score only considers the most recent uninterrupted run of price
    increases above `min_step_change_pct`.
    """
    if not math.isfinite(min_step_change_pct) or min_step_change_pct < 0:
        raise ValueError("min_step_change_pct must be a finite percentage >= 0")

    prices = _normalize_prices(price_points)
    if len(prices) < 2:
        return 0.0

    return _momentum_stats(prices, min_step_change_pct=min_step_change_pct)["score"]


def _build_signal(
    *,
    pair: str,
    latest_point: PricePoint,
    history_points: int,
    min_step_change_pct: float,
    scale_stages: int,
    stats: dict[str, float],
) -> dict[str, Any]:
    score = stats["score"]
    target_position_fraction = _target_position_fraction(score)

    return {
        "timestamp": datetime.utcfromtimestamp(latest_point.timestamp).isoformat(),
        "strategy": "momentum",
        "pair": pair,
        "price": latest_point.price,
        "direction": "UP",
        "action": "BUY",
        "side": "LONG",
        "reason": "consecutive_upward_momentum",
        "momentum_score": score,
        "consecutive_increases": int(stats["streak_count"]),
        "latest_change_pct": stats["latest_change_pct"],
        "average_step_change_pct": stats["average_step_change_pct"],
        "cumulative_change_pct": stats["cumulative_change_pct"],
        "threshold_pct": min_step_change_pct,
        "target_position_fraction": target_position_fraction,
        "entry_style": "scale_in",
        "scale_plan": _scale_plan(
            target_position_fraction=target_position_fraction,
            scale_stages=scale_stages,
            min_step_change_pct=min_step_change_pct,
        ),
        "data_points": history_points,
    }


def scan_for_signals(
    feeds: Iterable[Any],
    *,
    min_consecutive_increases: int = DEFAULT_MIN_CONSECUTIVE_INCREASES,
    min_step_change_pct: float = DEFAULT_MIN_STEP_CHANGE_PCT,
    scale_stages: int = DEFAULT_SCALE_STAGES,
) -> List[dict[str, Any]]:
    """
    Scan existing `PriceFeed.history` windows for trailing upward momentum signals.

    A signal is produced when the latest observed prices show at least
    `min_consecutive_increases` consecutive gains and each gain is greater than
    or equal to `min_step_change_pct`.
    """
    if min_consecutive_increases < 3:
        raise ValueError("min_consecutive_increases must be at least 3")
    if not math.isfinite(min_step_change_pct) or min_step_change_pct < 0:
        raise ValueError("min_step_change_pct must be a finite percentage >= 0")
    if scale_stages < 2:
        raise ValueError("scale_stages must be at least 2")

    signals: List[dict[str, Any]] = []

    for feed in feeds:
        history = list(getattr(feed, "history", []))
        if len(history) < min_consecutive_increases + 1:
            continue

        try:
            prices = _normalize_prices(history)
        except ValueError:
            continue

        stats = _momentum_stats(prices, min_step_change_pct=min_step_change_pct)
        if int(stats["streak_count"]) < min_consecutive_increases:
            continue

        signals.append(
            _build_signal(
                pair=str(getattr(feed, "pair_name", "unknown")),
                latest_point=history[-1],
                history_points=len(history),
                min_step_change_pct=min_step_change_pct,
                scale_stages=scale_stages,
                stats=stats,
            )
        )

    return signals


__all__ = ["momentum_score", "scan_for_signals"]
