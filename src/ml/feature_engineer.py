"""Feature extraction helpers for ML models fed by `PriceFeed.history`."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
from typing import Any, Iterable, Sequence

from ..oracle import PriceFeed

DEFAULT_SMA_PERIODS = (5, 10, 20, 50)


@dataclass(frozen=True)
class FeatureConfig:
    """Configuration for the dependency-free feature extraction pipeline."""

    rsi_period: int = 14
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9
    bollinger_period: int = 20
    bollinger_stddev_multiplier: float = 2.0
    volume_ratio_period: int = 20
    momentum_window: int = 5
    volatility_window: int = 10
    volatility_lookback: int = 20
    sma_periods: tuple[int, ...] = DEFAULT_SMA_PERIODS

    def __post_init__(self) -> None:
        """Validate configuration values early."""
        positive_int_fields = (
            "rsi_period",
            "macd_fast_period",
            "macd_slow_period",
            "macd_signal_period",
            "bollinger_period",
            "volume_ratio_period",
            "momentum_window",
            "volatility_window",
            "volatility_lookback",
        )
        for field_name in positive_int_fields:
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be an integer >= 1")

        if (
            not math.isfinite(self.bollinger_stddev_multiplier)
            or self.bollinger_stddev_multiplier <= 0
        ):
            raise ValueError("bollinger_stddev_multiplier must be a finite float > 0")

        if not self.sma_periods:
            raise ValueError("sma_periods must contain at least one period")
        if any(not isinstance(period, int) or period < 1 for period in self.sma_periods):
            raise ValueError("sma_periods must only contain integers >= 1")


DEFAULT_CONFIG = FeatureConfig()


def extract_features(
    feed: PriceFeed | Any,
    *,
    config: FeatureConfig | None = None,
) -> dict[str, float]:
    """
    Extract a stable numeric feature vector from a `PriceFeed`-compatible object.

    The extractor intentionally returns neutral defaults for short or sparse
    histories so downstream model code does not need to handle missing values.
    """
    return extract_features_from_history(getattr(feed, "history", []), config=config)


def extract_features_from_history(
    history: Iterable[Any],
    *,
    config: FeatureConfig | None = None,
) -> dict[str, float]:
    """Extract features directly from an iterable of `PricePoint`-like objects."""
    cfg = config or DEFAULT_CONFIG
    prices, volumes = _extract_series(history)

    macd_line, macd_signal, macd_histogram = _macd(
        prices,
        fast_period=cfg.macd_fast_period,
        slow_period=cfg.macd_slow_period,
        signal_period=cfg.macd_signal_period,
    )

    features = {
        "rsi": _rsi(prices, period=cfg.rsi_period),
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_histogram": macd_histogram,
        "bollinger_band_width": _bollinger_band_width(
            prices,
            window=cfg.bollinger_period,
            stddev_multiplier=cfg.bollinger_stddev_multiplier,
        ),
        "volume_ratio": _volume_ratio(volumes, period=cfg.volume_ratio_period),
        "momentum_score": _momentum_score(prices, window=cfg.momentum_window),
        "volatility_percentile": _volatility_percentile(
            prices,
            window=cfg.volatility_window,
            lookback=cfg.volatility_lookback,
        ),
    }
    features.update(_price_vs_sma_features(prices, cfg.sma_periods))
    return features


def extract_features_batch(
    feeds: Iterable[PriceFeed | Any],
    *,
    config: FeatureConfig | None = None,
) -> list[dict[str, float]]:
    """Extract one feature row per feed."""
    return [extract_features(feed, config=config) for feed in feeds]


def feature_names(config: FeatureConfig | None = None) -> list[str]:
    """Return the deterministic feature order produced by this module."""
    cfg = config or DEFAULT_CONFIG
    base_names = [
        "rsi",
        "macd_line",
        "macd_signal",
        "macd_histogram",
        "bollinger_band_width",
        "volume_ratio",
        "momentum_score",
        "volatility_percentile",
    ]
    base_names.extend(f"price_vs_sma_{period}" for period in cfg.sma_periods)
    return base_names


def _extract_series(history: Iterable[Any]) -> tuple[list[float], list[float]]:
    """Normalize history into aligned price and volume series."""
    prices: list[float] = []
    volumes: list[float] = []

    for point in history:
        price = float(getattr(point, "price", 0.0) or 0.0)
        if not math.isfinite(price) or price <= 0:
            continue

        volume = float(getattr(point, "volume_estimate", 0.0) or 0.0)
        if not math.isfinite(volume) or volume < 0:
            volume = 0.0

        prices.append(price)
        volumes.append(volume)

    return prices, volumes


def _mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean for non-empty sequences."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _population_stddev(values: Sequence[float]) -> float:
    """Return the population standard deviation."""
    if len(values) < 2:
        return 0.0

    mean_value = _mean(values)
    variance = sum((value - mean_value) ** 2 for value in values) / len(values)
    return variance**0.5


def _returns(prices: Sequence[float]) -> list[float]:
    """Return simple decimal returns between consecutive prices."""
    if len(prices) < 2:
        return []

    returns: list[float] = []
    for index in range(1, len(prices)):
        previous_price = prices[index - 1]
        if previous_price <= 0:
            continue
        returns.append((prices[index] - previous_price) / previous_price)
    return returns


def _pct_changes(prices: Sequence[float]) -> list[float]:
    """Return percentage price changes between consecutive prices."""
    return [value * 100.0 for value in _returns(prices)]


def _ema_series(values: Sequence[float], *, period: int) -> list[float]:
    """Return a same-length EMA series seeded from the first observation."""
    if not values:
        return []

    multiplier = 2.0 / (period + 1.0)
    ema = values[0]
    series = [ema]

    for value in values[1:]:
        ema = ((value - ema) * multiplier) + ema
        series.append(ema)

    return series


def _rsi(prices: Sequence[float], *, period: int) -> float:
    """Return RSI on the standard 0-100 scale."""
    changes = [current - previous for previous, current in zip(prices, prices[1:])]
    if not changes:
        return 50.0

    if len(changes) < period:
        gains = [max(change, 0.0) for change in changes]
        losses = [max(-change, 0.0) for change in changes]
        avg_gain = _mean(gains)
        avg_loss = _mean(losses)
    else:
        seed_changes = changes[:period]
        avg_gain = _mean([max(change, 0.0) for change in seed_changes])
        avg_loss = _mean([max(-change, 0.0) for change in seed_changes])

        for change in changes[period:]:
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0.0:
        if avg_gain == 0.0:
            return 50.0
        return 100.0

    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _macd(
    prices: Sequence[float],
    *,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> tuple[float, float, float]:
    """Return MACD line, signal line, and histogram."""
    if not prices:
        return 0.0, 0.0, 0.0

    fast_ema = _ema_series(prices, period=fast_period)
    slow_ema = _ema_series(prices, period=slow_period)
    macd_series = [fast - slow for fast, slow in zip(fast_ema, slow_ema)]
    signal_series = _ema_series(macd_series, period=signal_period)

    macd_line = macd_series[-1] if macd_series else 0.0
    signal_line = signal_series[-1] if signal_series else 0.0
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger_band_width(
    prices: Sequence[float],
    *,
    window: int,
    stddev_multiplier: float,
) -> float:
    """Return Bollinger Band width normalized by the moving average."""
    if len(prices) < 2:
        return 0.0

    recent_prices = list(prices[-min(window, len(prices)) :])
    moving_average = _mean(recent_prices)
    if moving_average <= 0:
        return 0.0

    stddev = _population_stddev(recent_prices)
    upper_band = moving_average + (stddev_multiplier * stddev)
    lower_band = moving_average - (stddev_multiplier * stddev)
    return (upper_band - lower_band) / moving_average


def _volume_ratio(volumes: Sequence[float], *, period: int) -> float:
    """
    Return latest volume divided by trailing average volume.

    A neutral ratio of 1.0 is returned when the oracle history has no usable
    volume estimates. This keeps feature rows stable even for pure Jupiter quotes.
    """
    if len(volumes) < 2:
        return 1.0

    recent_volumes = list(volumes[-(period + 1) :])
    latest_volume = recent_volumes[-1]
    baseline = recent_volumes[:-1]
    if not baseline:
        return 1.0

    average_volume = _mean(baseline)
    if average_volume <= 0:
        return 1.0

    return latest_volume / average_volume


def _momentum_score(prices: Sequence[float], *, window: int) -> float:
    """Return a trailing momentum score from the current positive streak."""
    recent_prices = list(prices[-(window + 1) :])
    changes_pct = _pct_changes(recent_prices)
    if not changes_pct:
        return 0.0

    streak: list[float] = []
    for change_pct in reversed(changes_pct):
        if change_pct > 0:
            streak.append(change_pct)
            continue
        break

    if not streak:
        return 0.0

    streak.reverse()
    average_increase_pct = _mean(streak)
    return len(streak) * average_increase_pct


def _volatility_percentile(
    prices: Sequence[float],
    *,
    window: int,
    lookback: int,
) -> float:
    """Return the percentile rank of current rolling volatility on a 0-1 scale."""
    returns = _returns(prices)
    effective_window = min(window, len(returns))
    if effective_window < 2:
        return 0.5

    volatility_series: list[float] = []
    for end_index in range(effective_window, len(returns) + 1):
        window_returns = returns[end_index - effective_window : end_index]
        volatility_series.append(_population_stddev(window_returns))

    if not volatility_series:
        return 0.5

    recent_volatility = volatility_series[-lookback:]
    latest_volatility = recent_volatility[-1]
    return _percentile_rank(recent_volatility, latest_volatility)


def _percentile_rank(values: Sequence[float], target: float) -> float:
    """Return percentile rank on a 0-1 scale with midpoint tie handling."""
    if not values:
        return 0.5

    sorted_values = sorted(values)
    lower = bisect_left(sorted_values, target)
    upper = bisect_right(sorted_values, target)
    return ((lower + upper) / 2.0) / len(sorted_values)


def _price_vs_sma_features(
    prices: Sequence[float],
    periods: Sequence[int],
) -> dict[str, float]:
    """Return latest-price-to-SMA ratios for the requested lookback periods."""
    if not prices:
        return {f"price_vs_sma_{period}": 1.0 for period in periods}

    latest_price = prices[-1]
    features: dict[str, float] = {}

    for period in periods:
        recent_prices = prices[-min(period, len(prices)) :]
        sma = _mean(recent_prices)
        features[f"price_vs_sma_{period}"] = latest_price / sma if sma > 0 else 1.0

    return features


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_SMA_PERIODS",
    "FeatureConfig",
    "extract_features",
    "extract_features_batch",
    "extract_features_from_history",
    "feature_names",
]
