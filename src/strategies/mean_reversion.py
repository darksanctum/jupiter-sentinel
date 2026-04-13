"""
Mean-reversion strategy based on Bollinger Bands over oracle price history.
"""

from __future__ import annotations
import logging

import math
from datetime import datetime
from typing import Any, Iterable, List

from ..oracle import PricePoint

DEFAULT_BOLLINGER_WINDOW = 20
DEFAULT_STDDEV_MULTIPLIER = 2.0


def _mean(values: Iterable[float]) -> float:
    """Function docstring."""
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _population_stddev(values: Iterable[float]) -> float:
    """Function docstring."""
    values = list(values)
    if len(values) < 2:
        return 0.0

    mean_value = _mean(values)
    variance = sum((value - mean_value) ** 2 for value in values) / len(values)
    return variance**0.5


def _build_signal(
    *,
    pair: str,
    latest_point: PricePoint,
    moving_average: float,
    lower_band: float,
    upper_band: float,
    stddev: float,
    window: int,
    stddev_multiplier: float,
    history_points: int,
) -> dict[str, Any]:
    """Function docstring."""
    direction = "DOWN" if latest_point.price < lower_band else "UP"
    action = "BUY" if direction == "DOWN" else "SELL"
    side = "LONG" if direction == "DOWN" else "SHORT"
    deviation = latest_point.price - moving_average
    deviation_pct = (deviation / moving_average) * 100 if moving_average else 0.0
    z_score = deviation / stddev if stddev else 0.0

    return {
        "timestamp": datetime.utcfromtimestamp(latest_point.timestamp).isoformat(),
        "strategy": "mean_reversion",
        "pair": pair,
        "price": latest_point.price,
        "moving_average": moving_average,
        "lower_band": lower_band,
        "upper_band": upper_band,
        "stddev": stddev,
        "z_score": z_score,
        "deviation_pct": deviation_pct,
        "direction": direction,
        "action": action,
        "side": side,
        "target_price": moving_average,
        "reason": (
            "price_below_lower_band"
            if direction == "DOWN"
            else "price_above_upper_band"
        ),
        "window": window,
        "stddev_multiplier": stddev_multiplier,
        "data_points": history_points,
    }


def scan_for_signals(
    feeds: Iterable[Any],
    *,
    window: int = DEFAULT_BOLLINGER_WINDOW,
    stddev_multiplier: float = DEFAULT_STDDEV_MULTIPLIER,
    min_bandwidth_pct: float = 0.0,
) -> List[dict[str, Any]]:
    """
    Scan existing `PriceFeed.history` windows for Bollinger Band mean-reversion signals.

    A signal is produced when the latest observed price moves outside the upper or
    lower Bollinger Band. Prices below the lower band yield a LONG/BUY signal,
    while prices above the upper band yield a SHORT/SELL signal.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    if not math.isfinite(stddev_multiplier) or stddev_multiplier <= 0:
        raise ValueError("stddev_multiplier must be a positive finite number")
    if not math.isfinite(min_bandwidth_pct) or min_bandwidth_pct < 0:
        raise ValueError("min_bandwidth_pct must be a finite percentage >= 0")

    signals: List[dict[str, Any]] = []

    for feed in feeds:
        history = list(getattr(feed, "history", []))
        if len(history) < window:
            continue

        recent_points = history[-window:]
        prices = []
        invalid_price = False

        for point in recent_points:
            price = float(getattr(point, "price", 0.0) or 0.0)
            if not math.isfinite(price) or price <= 0:
                invalid_price = True
                break
            prices.append(price)

        if invalid_price or len(prices) < window:
            continue

        moving_average = _mean(prices)
        stddev = _population_stddev(prices)
        if moving_average <= 0 or stddev <= 0:
            continue

        upper_band = moving_average + (stddev_multiplier * stddev)
        lower_band = moving_average - (stddev_multiplier * stddev)
        bandwidth_pct = ((upper_band - lower_band) / moving_average) * 100
        if bandwidth_pct < min_bandwidth_pct:
            continue

        latest_point = recent_points[-1]
        latest_price = float(getattr(latest_point, "price", 0.0) or 0.0)
        if lower_band < latest_price < upper_band:
            continue

        signals.append(
            _build_signal(
                pair=str(getattr(feed, "pair_name", "unknown")),
                latest_point=latest_point,
                moving_average=moving_average,
                lower_band=lower_band,
                upper_band=upper_band,
                stddev=stddev,
                window=window,
                stddev_multiplier=stddev_multiplier,
                history_points=len(history),
            )
        )

    return signals


__all__ = ["scan_for_signals"]
