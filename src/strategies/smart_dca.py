"""
Smart DCA strategy that adjusts recurring buy size using Bollinger Bands.
"""

from __future__ import annotations
import logging

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, List

from ..oracle import PricePoint
from .mean_reversion import DEFAULT_BOLLINGER_WINDOW, DEFAULT_STDDEV_MULTIPLIER

DEFAULT_BASE_AMOUNT = 1.0
DEFAULT_BUY_MULTIPLIER = 2.0


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


def _normalize_price_points(values: Iterable[Any]) -> List[PricePoint]:
    """Function docstring."""
    normalized: List[PricePoint] = []

    for index, value in enumerate(values):
        price = float(getattr(value, "price", value) or 0.0)
        timestamp = float(getattr(value, "timestamp", index))
        if not math.isfinite(price) or price <= 0:
            raise ValueError("prices must be positive finite numbers")
        normalized.append(PricePoint(timestamp=timestamp, price=price))

    return normalized


def _bollinger_bands(
    prices: List[float],
    *,
    window: int,
    stddev_multiplier: float,
) -> tuple[float, float, float, float]:
    """Function docstring."""
    recent_prices = prices[-window:]
    moving_average = _mean(recent_prices)
    stddev = _population_stddev(recent_prices)
    upper_band = moving_average + (stddev_multiplier * stddev)
    lower_band = moving_average - (stddev_multiplier * stddev)
    return moving_average, lower_band, upper_band, stddev


def _allocation_for_price(
    *,
    price: float,
    lower_band: float,
    upper_band: float,
    base_amount: float,
    multiplier: float,
) -> tuple[float, float, str]:
    """Function docstring."""
    if price < lower_band:
        allocation_multiplier = multiplier
        reason = "price_below_lower_band"
    elif price > upper_band:
        allocation_multiplier = 1.0 / multiplier
        reason = "price_above_upper_band"
    else:
        allocation_multiplier = 1.0
        reason = "base_dca"

    return base_amount * allocation_multiplier, allocation_multiplier, reason


@dataclass
class SmartDCAEntry:
    timestamp: float
    price: float
    amount: float
    token_received: float
    moving_average: float
    lower_band: float
    upper_band: float
    stddev: float
    allocation_multiplier: float
    reason: str
    accumulated_position: float = 0.0
    average_entry_price: float = 0.0


@dataclass
class SmartDCAState:
    pair: str
    base_amount: float
    multiplier: float
    total_invested: float = 0.0
    accumulated_position: float = 0.0
    average_entry_price: float = 0.0
    entries: List[SmartDCAEntry] = field(default_factory=list)

    def add_entry(self, entry: SmartDCAEntry) -> None:
        """Function docstring."""
        self.entries.append(entry)
        self.update_stats()
        entry.accumulated_position = self.accumulated_position
        entry.average_entry_price = self.average_entry_price

    def update_stats(self) -> None:
        """Function docstring."""
        if not self.entries:
            self.total_invested = 0.0
            self.accumulated_position = 0.0
            self.average_entry_price = 0.0
            return

        self.total_invested = sum(entry.amount for entry in self.entries)
        self.accumulated_position = sum(entry.token_received for entry in self.entries)
        if self.accumulated_position > 0:
            self.average_entry_price = self.total_invested / self.accumulated_position
        else:
            self.average_entry_price = 0.0


def simulate_smart_dca(
    feed_or_history: Any,
    *,
    base_amount: float = DEFAULT_BASE_AMOUNT,
    multiplier: float = DEFAULT_BUY_MULTIPLIER,
    window: int = DEFAULT_BOLLINGER_WINDOW,
    stddev_multiplier: float = DEFAULT_STDDEV_MULTIPLIER,
    pair: str | None = None,
) -> SmartDCAState:
    """
    Simulate recurring DCA buys that scale around Bollinger Bands.

    Before enough history is available to build bands, each buy uses the base
    amount. Once the trailing window is full, buys scale up to
    `base_amount * multiplier` below the lower band and scale down to
    `base_amount / multiplier` above the upper band.
    """
    if not math.isfinite(base_amount) or base_amount <= 0:
        raise ValueError("base_amount must be a positive finite number")
    if not math.isfinite(multiplier) or multiplier < 1:
        raise ValueError("multiplier must be a finite number >= 1")
    if window < 2:
        raise ValueError("window must be at least 2")
    if not math.isfinite(stddev_multiplier) or stddev_multiplier <= 0:
        raise ValueError("stddev_multiplier must be a positive finite number")

    history = list(getattr(feed_or_history, "history", feed_or_history))
    state = SmartDCAState(
        pair=pair or str(getattr(feed_or_history, "pair_name", "unknown")),
        base_amount=base_amount,
        multiplier=multiplier,
    )

    normalized_points = _normalize_price_points(history)
    prices: List[float] = []

    for point in normalized_points:
        prices.append(point.price)
        moving_average = point.price
        lower_band = point.price
        upper_band = point.price
        band_stddev = 0.0
        amount = base_amount
        allocation_multiplier = 1.0
        reason = "insufficient_history"

        if len(prices) >= window:
            moving_average, lower_band, upper_band, band_stddev = _bollinger_bands(
                prices,
                window=window,
                stddev_multiplier=stddev_multiplier,
            )
            amount, allocation_multiplier, reason = _allocation_for_price(
                price=point.price,
                lower_band=lower_band,
                upper_band=upper_band,
                base_amount=base_amount,
                multiplier=multiplier,
            )

        state.add_entry(
            SmartDCAEntry(
                timestamp=point.timestamp,
                price=point.price,
                amount=amount,
                token_received=amount / point.price,
                moving_average=moving_average,
                lower_band=lower_band,
                upper_band=upper_band,
                stddev=band_stddev,
                allocation_multiplier=allocation_multiplier,
                reason=reason,
            )
        )

    return state


__all__ = ["SmartDCAEntry", "SmartDCAState", "simulate_smart_dca"]
