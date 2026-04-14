"""
Jupiter Sentinel - Portfolio Risk Manager
Tracks portfolio-level exposure, concentration, drawdown, and Kelly sizing.
"""

from __future__ import annotations
import logging

import math
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence


def _as_float(value: Any, default: float = 0.0) -> float:
    """Function docstring."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _is_mapping(value: Any) -> bool:
    """Function docstring."""
    return isinstance(value, Mapping)


def _read_field(payload: Any, key: str, default: Any = None) -> Any:
    """Function docstring."""
    if _is_mapping(payload):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _read_nested_pnl_pct(record: Any) -> Optional[float]:
    """Function docstring."""
    direct = _read_field(record, "pnl_pct")
    if direct is not None:
        value = _as_float(direct, float("nan"))
        if math.isfinite(value):
            return value

    action = _read_field(record, "action")
    if action is not None:
        nested = _read_field(action, "pnl_pct")
        if nested is not None:
            value = _as_float(nested, float("nan"))
            if math.isfinite(value):
                return value

    decimal_value = _read_field(record, "return_decimal")
    if decimal_value is not None:
        value = _as_float(decimal_value, float("nan"))
        if math.isfinite(value):
            return value * 100.0

    return None


def _history_prices(feed: Any, lookback: int) -> list[float]:
    """Function docstring."""
    history = _read_field(feed, "history", [])
    prices = [_as_float(_read_field(point, "price"), float("nan")) for point in history]
    clean = [price for price in prices if math.isfinite(price) and price > 0]
    if lookback > 0:
        return clean[-(lookback + 1) :]
    return clean


def _returns_from_prices(prices: Sequence[float]) -> list[float]:
    """Function docstring."""
    returns: list[float] = []
    for index in range(1, len(prices)):
        previous = prices[index - 1]
        current = prices[index]
        if previous <= 0:
            continue
        returns.append((current - previous) / previous)
    return returns


def _pearson_correlation(left: Sequence[float], right: Sequence[float]) -> float:
    """Function docstring."""
    sample_size = min(len(left), len(right))
    if sample_size < 2:
        return 0.0

    left_values = list(left[-sample_size:])
    right_values = list(right[-sample_size:])
    left_mean = math.fsum(left_values) / sample_size
    right_mean = math.fsum(right_values) / sample_size

    covariance = 0.0
    left_variance = 0.0
    right_variance = 0.0
    for left_value, right_value in zip(left_values, right_values):
        left_delta = left_value - left_mean
        right_delta = right_value - right_mean
        covariance += left_delta * right_delta
        left_variance += left_delta * left_delta
        right_variance += right_delta * right_delta

    if left_variance <= 0 or right_variance <= 0:
        return 0.0
    return covariance / math.sqrt(left_variance * right_variance)


class PortfolioRiskManager:
    """
    Manage risk at the portfolio level instead of per-position only.

    The manager keeps a sticky drawdown halt: once breached, new entries stay
    blocked until `reset_halt()` is called.
    """

    def __init__(
        self,
        *,
        max_drawdown_pct: float = 0.20,
        max_portfolio_exposure_pct: float = 1.0,
        correlation_lookback: int = 30,
        kelly_fraction_cap: float = 0.25,
    ) -> None:
        """Function docstring."""
        if not 0 < max_drawdown_pct < 1:
            raise ValueError("max_drawdown_pct must be between 0 and 1")
        if max_portfolio_exposure_pct <= 0:
            raise ValueError("max_portfolio_exposure_pct must be positive")
        if correlation_lookback < 2:
            raise ValueError("correlation_lookback must be at least 2")
        if not 0 < kelly_fraction_cap <= 1:
            raise ValueError("kelly_fraction_cap must be between 0 and 1")

        self.max_drawdown_pct = float(max_drawdown_pct)
        self.max_portfolio_exposure_pct = float(max_portfolio_exposure_pct)
        self.correlation_lookback = int(correlation_lookback)
        self.kelly_fraction_cap = float(kelly_fraction_cap)

        self.peak_portfolio_value_usd = 0.0
        self.current_portfolio_value_usd = 0.0
        self.current_drawdown_pct = 0.0
        self.halt_trading = False
        self.equity_history: list[dict[str, Any]] = []

    def reset_halt(self) -> None:
        """Manually clear the drawdown halt after review."""
        self.halt_trading = False

    def can_open_new_positions(self) -> bool:
        """Return whether portfolio-level guardrails allow new exposure."""
        return not self.halt_trading

    def _entry_value_usd(self, position: Any, *, sol_price: float = 0.0) -> float:
        """Function docstring."""
        notional = _as_float(_read_field(position, "notional"), 0.0)
        if notional > 0:
            return notional

        amount_sol = _as_float(_read_field(position, "amount_sol"), 0.0)
        if amount_sol <= 0 or sol_price <= 0:
            return 0.0
        return amount_sol * sol_price

    def _current_price(self, position: Any, price_feeds: Mapping[str, Any]) -> float:
        """Function docstring."""
        pair = str(_read_field(position, "pair", ""))
        feed = price_feeds.get(pair)
        if feed is None:
            return _as_float(_read_field(position, "entry_price"), 0.0)

        current_price = _as_float(_read_field(feed, "current_price"), 0.0)
        if current_price > 0:
            return current_price

        prices = _history_prices(feed, self.correlation_lookback)
        if prices:
            return prices[-1]
        return _as_float(_read_field(position, "entry_price"), 0.0)

    def _position_market_value_usd(
        self,
        position: Any,
        *,
        price_feeds: Optional[Mapping[str, Any]] = None,
        sol_price: float = 0.0,
    ) -> float:
        """Function docstring."""
        entry_value = self._entry_value_usd(position, sol_price=sol_price)
        if entry_value <= 0:
            return 0.0

        entry_price = _as_float(_read_field(position, "entry_price"), 0.0)
        if entry_price <= 0:
            return entry_value

        current_price = self._current_price(position, price_feeds or {})
        if current_price <= 0:
            return entry_value

        return max(entry_value * (current_price / entry_price), 0.0)

    def calculate_total_exposure(
        self,
        positions: Sequence[Any],
        *,
        price_feeds: Optional[Mapping[str, Any]] = None,
        sol_price: float = 0.0,
    ) -> float:
        """Return gross mark-to-market exposure across open positions."""
        return math.fsum(
            self._position_market_value_usd(
                position, price_feeds=price_feeds, sol_price=sol_price
            )
            for position in positions
            if str(_read_field(position, "status", "open")) == "open"
        )

    def calculate_position_correlations(
        self,
        positions: Sequence[Any],
        price_feeds: Mapping[str, Any],
    ) -> dict[str, dict[str, float]]:
        """Build a pairwise correlation matrix from feed return histories."""
        open_positions = [
            position
            for position in positions
            if str(_read_field(position, "status", "open")) == "open"
        ]
        correlations: dict[str, dict[str, float]] = {}

        for left_index, left_position in enumerate(open_positions):
            left_pair = str(_read_field(left_position, "pair", ""))
            correlations.setdefault(left_pair, {})
            correlations[left_pair][left_pair] = 1.0
            left_feed = price_feeds.get(left_pair)
            left_returns = (
                _returns_from_prices(
                    _history_prices(left_feed, self.correlation_lookback)
                )
                if left_feed
                else []
            )

            for right_position in open_positions[left_index + 1 :]:
                right_pair = str(_read_field(right_position, "pair", ""))
                correlations.setdefault(right_pair, {})
                correlations[right_pair][right_pair] = 1.0
                right_feed = price_feeds.get(right_pair)
                right_returns = (
                    _returns_from_prices(
                        _history_prices(right_feed, self.correlation_lookback)
                    )
                    if right_feed
                    else []
                )

                corr = _pearson_correlation(left_returns, right_returns)
                correlations[left_pair][right_pair] = corr
                correlations[right_pair][left_pair] = corr

        return correlations

    def average_correlation(
        self,
        positions: Sequence[Any],
        price_feeds: Mapping[str, Any],
        *,
        sol_price: float = 0.0,
    ) -> float:
        """Return exposure-weighted average absolute correlation across open positions."""
        open_positions = [
            position
            for position in positions
            if str(_read_field(position, "status", "open")) == "open"
        ]
        if len(open_positions) < 2:
            return 0.0

        correlations = self.calculate_position_correlations(open_positions, price_feeds)
        weighted_total = 0.0
        weight_sum = 0.0

        for left_index, left_position in enumerate(open_positions):
            left_pair = str(_read_field(left_position, "pair", ""))
            left_exposure = self._position_market_value_usd(
                left_position,
                price_feeds=price_feeds,
                sol_price=sol_price,
            )
            for right_position in open_positions[left_index + 1 :]:
                right_pair = str(_read_field(right_position, "pair", ""))
                right_exposure = self._position_market_value_usd(
                    right_position,
                    price_feeds=price_feeds,
                    sol_price=sol_price,
                )
                weight = left_exposure * right_exposure
                if weight <= 0:
                    continue
                weighted_total += (
                    abs(correlations.get(left_pair, {}).get(right_pair, 0.0)) * weight
                )
                weight_sum += weight

        if weight_sum <= 0:
            return 0.0
        return weighted_total / weight_sum

    def estimate_portfolio_value(
        self,
        positions: Sequence[Any],
        *,
        price_feeds: Optional[Mapping[str, Any]] = None,
        cash_usd: float = 0.0,
        sol_price: float = 0.0,
    ) -> float:
        """Estimate total equity as cash plus marked-to-market open positions."""
        return _as_float(cash_usd) + self.calculate_total_exposure(
            positions,
            price_feeds=price_feeds,
            sol_price=sol_price,
        )

    def update_drawdown(
        self,
        portfolio_value_usd: float,
        *,
        timestamp: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Track peak-to-current drawdown and halt if the configured limit is breached."""
        current_value = max(_as_float(portfolio_value_usd), 0.0)
        self.current_portfolio_value_usd = current_value
        self.peak_portfolio_value_usd = max(
            self.peak_portfolio_value_usd, current_value
        )

        peak = self.peak_portfolio_value_usd
        drawdown = ((peak - current_value) / peak) if peak > 0 else 0.0
        self.current_drawdown_pct = max(drawdown, 0.0)
        if self.current_drawdown_pct >= self.max_drawdown_pct:
            self.halt_trading = True

        record = {
            "timestamp": timestamp or datetime.utcnow().isoformat(),
            "portfolio_value_usd": current_value,
            "peak_portfolio_value_usd": peak,
            "drawdown_pct": self.current_drawdown_pct * 100.0,
            "halt_trading": self.halt_trading,
        }
        self.equity_history.append(record)
        return record

    def calculate_kelly_criterion(
        self,
        trade_history: Optional[Iterable[Any]] = None,
        *,
        closed_positions: Optional[Iterable[Any]] = None,
    ) -> dict[str, float]:
        """
        Compute Kelly sizing inputs from realized trade outcomes.

        The method accepts mixed schemas and only uses records that expose a
        realized `pnl_pct` either directly or under `action.pnl_pct`.
        """
        realized_returns: list[float] = []

        for record in trade_history or []:
            pnl_pct = _read_nested_pnl_pct(record)
            if pnl_pct is not None:
                realized_returns.append(pnl_pct)

        for record in closed_positions or []:
            pnl_pct = _read_nested_pnl_pct(record)
            if pnl_pct is not None:
                realized_returns.append(pnl_pct)

        wins = [value for value in realized_returns if value > 0]
        losses = [abs(value) for value in realized_returns if value < 0]
        total = len(realized_returns)
        win_rate = (len(wins) / total) if total else 0.0
        avg_win = math.fsum(wins) / len(wins) if wins else 0.0
        avg_loss = math.fsum(losses) / len(losses) if losses else 0.0
        win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        if win_rate <= 0 or win_loss_ratio <= 0:
            raw_kelly = 0.0
        else:
            raw_kelly = max(win_rate - ((1.0 - win_rate) / win_loss_ratio), 0.0)

        return {
            "trades_used": float(total),
            "win_rate": win_rate,
            "win_rate_pct": win_rate * 100.0,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "win_loss_ratio": win_loss_ratio,
            "kelly_fraction": raw_kelly,
            "capped_kelly_fraction": min(raw_kelly, self.kelly_fraction_cap),
        }

    def recommend_position_size(
        self,
        portfolio_value_usd: float,
        trade_history: Optional[Iterable[Any]] = None,
        *,
        closed_positions: Optional[Iterable[Any]] = None,
        positions: Optional[Sequence[Any]] = None,
        price_feeds: Optional[Mapping[str, Any]] = None,
        sol_price: float = 0.0,
    ) -> dict[str, float]:
        """
        Recommend a new position size in USD using capped Kelly and portfolio crowding.
        """
        kelly = self.calculate_kelly_criterion(
            trade_history, closed_positions=closed_positions
        )
        open_positions = list(positions or [])
        feeds = price_feeds or {}
        current_exposure = self.calculate_total_exposure(
            open_positions, price_feeds=feeds, sol_price=sol_price
        )
        average_correlation = self.average_correlation(
            open_positions, feeds, sol_price=sol_price
        )
        correlation_adjustment = 1.0 / (1.0 + average_correlation)
        adjusted_fraction = kelly["capped_kelly_fraction"] * correlation_adjustment

        portfolio_value = max(_as_float(portfolio_value_usd), 0.0)
        max_exposure = portfolio_value * self.max_portfolio_exposure_pct
        available_exposure = max(max_exposure - current_exposure, 0.0)
        recommended_size = min(portfolio_value * adjusted_fraction, available_exposure)
        if self.halt_trading:
            recommended_size = 0.0

        return {
            **kelly,
            "average_correlation": average_correlation,
            "correlation_adjustment": correlation_adjustment,
            "adjusted_kelly_fraction": adjusted_fraction,
            "current_exposure_usd": current_exposure,
            "available_exposure_usd": available_exposure,
            "recommended_position_usd": recommended_size,
        }

    def portfolio_snapshot(
        self,
        positions: Sequence[Any],
        *,
        price_feeds: Optional[Mapping[str, Any]] = None,
        trade_history: Optional[Iterable[Any]] = None,
        closed_positions: Optional[Iterable[Any]] = None,
        portfolio_value_usd: Optional[float] = None,
        cash_usd: float = 0.0,
        sol_price: float = 0.0,
        timestamp: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Return a consolidated portfolio risk snapshot."""
        feeds = price_feeds or {}
        current_value = (
            self.estimate_portfolio_value(
                positions, price_feeds=feeds, cash_usd=cash_usd, sol_price=sol_price
            )
            if portfolio_value_usd is None
            else _as_float(portfolio_value_usd)
        )
        exposure = self.calculate_total_exposure(
            positions, price_feeds=feeds, sol_price=sol_price
        )
        drawdown = self.update_drawdown(current_value, timestamp=timestamp)
        sizing = self.recommend_position_size(
            current_value,
            trade_history,
            closed_positions=closed_positions,
            positions=positions,
            price_feeds=feeds,
            sol_price=sol_price,
        )
        max_exposure = current_value * self.max_portfolio_exposure_pct

        return {
            "portfolio_value_usd": current_value,
            "peak_portfolio_value_usd": self.peak_portfolio_value_usd,
            "drawdown_pct": drawdown["drawdown_pct"],
            "max_drawdown_limit_pct": self.max_drawdown_pct * 100.0,
            "halt_trading": self.halt_trading,
            "open_positions": sum(
                1
                for position in positions
                if str(_read_field(position, "status", "open")) == "open"
            ),
            "total_exposure_usd": exposure,
            "exposure_utilization_pct": (
                (exposure / max_exposure * 100.0) if max_exposure > 0 else 0.0
            ),
            "correlations": self.calculate_position_correlations(positions, feeds),
            **sizing,
        }


__all__ = ["PortfolioRiskManager"]
