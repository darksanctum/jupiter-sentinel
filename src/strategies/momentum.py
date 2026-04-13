"""
Momentum strategy based on consecutive upward price moves over oracle history.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, List, Sequence

from ..oracle import PricePoint

DEFAULT_MIN_CONSECUTIVE_INCREASES = 3
DEFAULT_MIN_INCREASE_PCT = 0.5
DEFAULT_SCALE_STEPS = 4


def _extract_prices(values: Iterable[Any]) -> List[float]:
    prices: List[float] = []

    for value in values:
        price = float(getattr(value, "price", value) or 0.0)
        if not math.isfinite(price) or price <= 0:
            return []
        prices.append(price)

    return prices


def _price_changes_pct(prices: Sequence[float]) -> List[float]:
    changes: List[float] = []

    for index in range(1, len(prices)):
        previous_price = prices[index - 1]
        current_price = prices[index]
        if previous_price <= 0:
            return []
        changes.append(((current_price - previous_price) / previous_price) * 100)

    return changes


def _qualifying_streak(changes_pct: Sequence[float], *, min_increase_pct: float) -> List[float]:
    streak: List[float] = []

    for change_pct in reversed(changes_pct):
        if change_pct >= min_increase_pct:
            streak.append(change_pct)
            continue
        break

    streak.reverse()
    return streak


def momentum_score(
    prices: Iterable[Any],
    *,
    min_increase_pct: float = DEFAULT_MIN_INCREASE_PCT,
) -> float:
    """
    Return a simple trailing momentum score based on the active qualifying streak.

    The score is the trailing streak length multiplied by the streak's average
    percentage gain. Non-qualifying trailing moves contribute a score of 0.
    """
    if not math.isfinite(min_increase_pct) or min_increase_pct < 0:
        raise ValueError("min_increase_pct must be a finite percentage >= 0")

    normalized_prices = _extract_prices(prices)
    if len(normalized_prices) < 2:
        return 0.0

    changes_pct = _price_changes_pct(normalized_prices)
    if not changes_pct:
        return 0.0

    streak_changes = _qualifying_streak(changes_pct, min_increase_pct=min_increase_pct)
    if not streak_changes:
        return 0.0

    average_increase_pct = sum(streak_changes) / len(streak_changes)
    return len(streak_changes) * average_increase_pct


def _build_signal(
    *,
    pair: str,
    latest_point: PricePoint,
    anchor_price: float,
    score: float,
    consecutive_increases: int,
    streak_changes: Sequence[float],
    min_consecutive_increases: int,
    min_increase_pct: float,
    max_scale_steps: int,
    history_points: int,
) -> dict[str, Any]:
    average_increase_pct = sum(streak_changes) / consecutive_increases
    total_change_pct = ((latest_point.price - anchor_price) / anchor_price) * 100 if anchor_price else 0.0
    scale_step = min(max_scale_steps, 1 + max(0, consecutive_increases - min_consecutive_increases))

    return {
        "timestamp": datetime.utcfromtimestamp(latest_point.timestamp).isoformat(),
        "strategy": "momentum",
        "pair": pair,
        "price": latest_point.price,
        "direction": "UP",
        "action": "BUY",
        "side": "LONG",
        "reason": "consecutive_price_increases",
        "momentum_score": score,
        "consecutive_increases": consecutive_increases,
        "recent_changes_pct": list(streak_changes),
        "average_increase_pct": average_increase_pct,
        "cumulative_change_pct": total_change_pct,
        "starting_price": anchor_price,
        "entry_mode": "SCALE_IN",
        "scale_step": scale_step,
        "scale_steps_total": max_scale_steps,
        "incremental_allocation_fraction": 1 / max_scale_steps,
        "allocation_fraction": scale_step / max_scale_steps,
        "min_consecutive_increases": min_consecutive_increases,
        "min_increase_pct": min_increase_pct,
        "data_points": history_points,
    }


def scan_for_signals(
    feeds: Iterable[Any],
    *,
    min_consecutive_increases: int = DEFAULT_MIN_CONSECUTIVE_INCREASES,
    min_increase_pct: float = DEFAULT_MIN_INCREASE_PCT,
    max_scale_steps: int = DEFAULT_SCALE_STEPS,
) -> List[dict[str, Any]]:
    """
    Scan `PriceFeed.history` sequences for strong upward momentum.

    A signal is emitted when the trailing price series ends with at least
    `min_consecutive_increases` consecutive gains, and each gain is at least
    `min_increase_pct`. Signals recommend scaling in over `max_scale_steps`
    tranches instead of taking full size immediately.
    """
    if min_consecutive_increases < 1:
        raise ValueError("min_consecutive_increases must be at least 1")
    if not math.isfinite(min_increase_pct) or min_increase_pct < 0:
        raise ValueError("min_increase_pct must be a finite percentage >= 0")
    if max_scale_steps < 1:
        raise ValueError("max_scale_steps must be at least 1")

    signals: List[dict[str, Any]] = []

    for feed in feeds:
        history = list(getattr(feed, "history", []))
        if len(history) < min_consecutive_increases + 1:
            continue

        prices = _extract_prices(history)
        if len(prices) != len(history):
            continue

        changes_pct = _price_changes_pct(prices)
        if not changes_pct:
            continue

        streak_changes = _qualifying_streak(changes_pct, min_increase_pct=min_increase_pct)
        consecutive_increases = len(streak_changes)
        if consecutive_increases < min_consecutive_increases:
            continue

        latest_point = history[-1]
        anchor_point = history[-(consecutive_increases + 1)]
        score = momentum_score(prices, min_increase_pct=min_increase_pct)
        signals.append(
            _build_signal(
                pair=str(getattr(feed, "pair_name", "unknown")),
                latest_point=latest_point,
                anchor_price=float(getattr(anchor_point, "price", 0.0) or 0.0),
                score=score,
                consecutive_increases=consecutive_increases,
                streak_changes=streak_changes,
                min_consecutive_increases=min_consecutive_increases,
                min_increase_pct=min_increase_pct,
                max_scale_steps=max_scale_steps,
                history_points=len(history),
            )
        )

    signals.sort(key=lambda signal: signal["momentum_score"], reverse=True)
    return signals


__all__ = ["momentum_score", "scan_for_signals"]
