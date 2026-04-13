"""Z-score based anomaly detection for unusual price, volume, and spread moves."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Iterable, Mapping, Sequence


class AnomalyKind(str, Enum):
    """High-level categories for abnormal market behavior."""

    NORMAL = "NORMAL"
    PUMP = "PUMP"
    DUMP = "DUMP"
    WHALE_MANIPULATION = "WHALE_MANIPULATION"
    MARKET_EVENT = "MARKET_EVENT"


@dataclass(frozen=True)
class AnomalyConfig:
    """Configuration for the z-score based anomaly detector."""

    lookback: int = 30
    min_history: int = 12
    return_z_threshold: float = 2.5
    volume_z_threshold: float = 2.5
    spread_z_threshold: float = 2.0
    min_absolute_return: float = 0.01

    def __post_init__(self) -> None:
        integer_fields = ("lookback", "min_history")
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 3:
                raise ValueError(f"{field_name} must be an integer >= 3")

        float_fields = (
            "return_z_threshold",
            "volume_z_threshold",
            "spread_z_threshold",
            "min_absolute_return",
        )
        for field_name in float_fields:
            try:
                value = float(getattr(self, field_name))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} must be a finite float >= 0") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be a finite float >= 0")


@dataclass(frozen=True)
class MetricSnapshot:
    """Latest metric value compared against its historical baseline."""

    value: float = 0.0
    mean: float = 0.0
    stddev: float = 0.0
    zscore: float = 0.0
    sample_size: int = 0
    is_outlier: bool = False

    @property
    def abs_zscore(self) -> float:
        """Return the absolute z-score."""
        return abs(self.zscore)


@dataclass(frozen=True)
class AnomalySignal:
    """Structured anomaly detection output for the latest observation."""

    flagged: bool
    kind: AnomalyKind
    severity: float
    pair_name: str | None
    timestamp: float | None
    history_size: int
    latest_price: float
    latest_return: float
    latest_volume: float
    latest_spread: float
    reasons: tuple[str, ...]
    return_snapshot: MetricSnapshot
    volume_snapshot: MetricSnapshot
    spread_snapshot: MetricSnapshot

    @property
    def triggered_metrics(self) -> tuple[str, ...]:
        """Return the metric names that triggered the anomaly."""
        metrics: list[str] = []
        if self.return_snapshot.is_outlier:
            metrics.append("returns")
        if self.volume_snapshot.is_outlier:
            metrics.append("volume")
        if self.spread_snapshot.is_outlier:
            metrics.append("spread")
        return tuple(metrics)

    @property
    def summary(self) -> str:
        """Return a short human-readable summary."""
        if not self.flagged:
            if self.reasons:
                return f"No anomaly detected ({', '.join(self.reasons)})."
            return "No anomaly detected."

        label = self.kind.value.replace("_", " ").title()
        return f"{label}: {'; '.join(self.reasons)}"


@dataclass(frozen=True)
class _NormalizedPoint:
    """Internal normalized market point."""

    timestamp: float | None
    price: float
    volume: float
    spread: float | None


DEFAULT_ANOMALY_CONFIG = AnomalyConfig()


class AnomalyDetector:
    """Detect anomalous market behavior from recent history."""

    def __init__(self, config: AnomalyConfig | None = None) -> None:
        """Store detector configuration."""
        self.config = config or DEFAULT_ANOMALY_CONFIG

    def detect(self, feed_or_history: Any) -> AnomalySignal:
        """Analyze the latest point in a feed or point history."""
        history = _coerce_history(feed_or_history)
        pair_name = _as_optional_text(_read_field(feed_or_history, "pair_name"))
        points = _normalize_points(history)
        price_series = [point.price for point in points]
        return_series = _returns(price_series)
        latest_return = return_series[-1] if return_series else 0.0

        latest_point = points[-1] if points else None
        if len(points) < self.config.min_history:
            return self._normal_signal(
                pair_name=pair_name,
                latest_point=latest_point,
                latest_return=latest_return,
                history_size=len(points),
                reasons=("insufficient_history",),
            )

        return_snapshot = _build_snapshot(
            return_series,
            lookback=self.config.lookback,
            threshold=self.config.return_z_threshold,
            min_abs_value=self.config.min_absolute_return,
            positive_only=False,
        )
        volume_snapshot = _build_snapshot(
            [point.volume for point in points],
            lookback=self.config.lookback,
            threshold=self.config.volume_z_threshold,
            min_abs_value=0.0,
            positive_only=True,
        )

        latest_spread = points[-1].spread
        if latest_spread is None:
            spread_series: list[float] = []
        else:
            spread_series = [
                point.spread for point in points[:-1] if point.spread is not None
            ]
            spread_series.append(latest_spread)

        spread_snapshot = _build_snapshot(
            spread_series,
            lookback=self.config.lookback,
            threshold=self.config.spread_z_threshold,
            min_abs_value=0.0,
            positive_only=True,
        )

        kind = _classify_signal(
            latest_return=latest_return,
            return_snapshot=return_snapshot,
            volume_snapshot=volume_snapshot,
            spread_snapshot=spread_snapshot,
        )
        reasons = _build_reasons(
            latest_return=latest_return,
            latest_volume=points[-1].volume,
            latest_spread=latest_spread or 0.0,
            return_snapshot=return_snapshot,
            volume_snapshot=volume_snapshot,
            spread_snapshot=spread_snapshot,
        )
        severity = _signal_severity(
            return_snapshot=return_snapshot,
            volume_snapshot=volume_snapshot,
            spread_snapshot=spread_snapshot,
        )

        if kind is AnomalyKind.NORMAL:
            return self._normal_signal(
                pair_name=pair_name,
                latest_point=points[-1],
                latest_return=latest_return,
                history_size=len(points),
                reasons=(),
                return_snapshot=return_snapshot,
                volume_snapshot=volume_snapshot,
                spread_snapshot=spread_snapshot,
            )

        return AnomalySignal(
            flagged=True,
            kind=kind,
            severity=severity,
            pair_name=pair_name,
            timestamp=points[-1].timestamp,
            history_size=len(points),
            latest_price=points[-1].price,
            latest_return=latest_return,
            latest_volume=points[-1].volume,
            latest_spread=latest_spread or 0.0,
            reasons=reasons,
            return_snapshot=return_snapshot,
            volume_snapshot=volume_snapshot,
            spread_snapshot=spread_snapshot,
        )

    def _normal_signal(
        self,
        *,
        pair_name: str | None,
        latest_point: _NormalizedPoint | None,
        latest_return: float,
        history_size: int,
        reasons: tuple[str, ...],
        return_snapshot: MetricSnapshot | None = None,
        volume_snapshot: MetricSnapshot | None = None,
        spread_snapshot: MetricSnapshot | None = None,
    ) -> AnomalySignal:
        """Build a non-flagged signal."""
        return AnomalySignal(
            flagged=False,
            kind=AnomalyKind.NORMAL,
            severity=0.0,
            pair_name=pair_name,
            timestamp=latest_point.timestamp if latest_point is not None else None,
            history_size=history_size,
            latest_price=latest_point.price if latest_point is not None else 0.0,
            latest_return=latest_return,
            latest_volume=latest_point.volume if latest_point is not None else 0.0,
            latest_spread=(
                latest_point.spread
                if latest_point is not None and latest_point.spread is not None
                else 0.0
            ),
            reasons=reasons,
            return_snapshot=return_snapshot or MetricSnapshot(),
            volume_snapshot=volume_snapshot or MetricSnapshot(),
            spread_snapshot=spread_snapshot or MetricSnapshot(),
        )


def detect_anomaly(
    feed_or_history: Any,
    *,
    config: AnomalyConfig | None = None,
) -> AnomalySignal:
    """Convenience wrapper around `AnomalyDetector.detect`."""
    return AnomalyDetector(config=config).detect(feed_or_history)


def detect_anomaly_from_history(
    history: Iterable[Any],
    *,
    config: AnomalyConfig | None = None,
) -> AnomalySignal:
    """Detect anomalies directly from a history iterable."""
    return detect_anomaly(list(history), config=config)


def _coerce_history(feed_or_history: Any) -> list[Any]:
    """Return a concrete history list from a feed-like object or raw iterable."""
    history = _read_field(feed_or_history, "history", feed_or_history)
    if history is None:
        return []
    return list(history)


def _normalize_points(history: Iterable[Any]) -> list[_NormalizedPoint]:
    """Extract valid price, volume, and spread observations from history."""
    normalized: list[_NormalizedPoint] = []
    for point in history:
        price = _as_float(_read_field(point, "price"), float("nan"))
        if not math.isfinite(price) or price <= 0:
            continue

        volume = _extract_volume(point)
        spread = _extract_spread(point)
        timestamp = _as_timestamp(_read_field(point, "timestamp"))
        normalized.append(
            _NormalizedPoint(
                timestamp=timestamp,
                price=price,
                volume=volume,
                spread=spread,
            )
        )
    return normalized


def _extract_volume(point: Any) -> float:
    """Read volume from common point field names."""
    for field_name in ("volume_estimate", "volume", "volume_usd"):
        value = _as_float(_read_field(point, field_name), float("nan"))
        if math.isfinite(value) and value >= 0:
            return value
    return 0.0


def _extract_spread(point: Any) -> float | None:
    """Read spread from direct fields or derive it from bid/ask quotes."""
    for field_name in ("spread", "spread_bps", "spread_pct"):
        value = _as_float(_read_field(point, field_name), float("nan"))
        if math.isfinite(value) and value >= 0:
            return value

    bid = _read_first_float(
        point,
        ("bid", "bid_price", "best_bid"),
        default=float("nan"),
    )
    ask = _read_first_float(
        point,
        ("ask", "ask_price", "best_ask"),
        default=float("nan"),
    )
    if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid:
        midpoint = (bid + ask) / 2.0
        if midpoint > 0:
            return (ask - bid) / midpoint
    return None


def _build_snapshot(
    series: Sequence[float],
    *,
    lookback: int,
    threshold: float,
    min_abs_value: float,
    positive_only: bool,
) -> MetricSnapshot:
    """Compare the latest series value to a trailing historical baseline."""
    if len(series) < 3:
        latest = float(series[-1]) if series else 0.0
        return MetricSnapshot(value=latest, sample_size=max(len(series) - 1, 0))

    window = list(series[-(lookback + 1) :])
    baseline = window[:-1]
    current = float(window[-1])
    mean = _mean(baseline)
    stddev = _population_stddev(baseline)
    zscore = _zscore(current, mean=mean, stddev=stddev, sample_size=len(baseline))

    if positive_only:
        is_outlier = current > min_abs_value and zscore >= threshold
    else:
        is_outlier = abs(current) >= min_abs_value and abs(zscore) >= threshold

    return MetricSnapshot(
        value=current,
        mean=mean,
        stddev=stddev,
        zscore=zscore,
        sample_size=len(baseline),
        is_outlier=is_outlier,
    )


def _classify_signal(
    *,
    latest_return: float,
    return_snapshot: MetricSnapshot,
    volume_snapshot: MetricSnapshot,
    spread_snapshot: MetricSnapshot,
) -> AnomalyKind:
    """Map metric outliers to a coarse anomaly label."""
    if return_snapshot.is_outlier and volume_snapshot.is_outlier:
        if latest_return > 0:
            return AnomalyKind.PUMP
        if latest_return < 0:
            return AnomalyKind.DUMP

    if spread_snapshot.is_outlier and (
        volume_snapshot.is_outlier or return_snapshot.is_outlier
    ):
        return AnomalyKind.WHALE_MANIPULATION

    if (
        return_snapshot.is_outlier
        or volume_snapshot.is_outlier
        or spread_snapshot.is_outlier
    ):
        return AnomalyKind.MARKET_EVENT

    return AnomalyKind.NORMAL


def _build_reasons(
    *,
    latest_return: float,
    latest_volume: float,
    latest_spread: float,
    return_snapshot: MetricSnapshot,
    volume_snapshot: MetricSnapshot,
    spread_snapshot: MetricSnapshot,
) -> tuple[str, ...]:
    """Generate short reasons describing the anomaly evidence."""
    reasons: list[str] = []

    if return_snapshot.is_outlier:
        direction = "upside" if latest_return > 0 else "downside"
        reasons.append(
            f"{direction} return outlier {latest_return:.2%} ({_format_zscore(return_snapshot.zscore)}z)"
        )
    if volume_snapshot.is_outlier:
        reasons.append(
            f"volume spike {latest_volume:.2f} ({_format_zscore(volume_snapshot.zscore)}z)"
        )
    if spread_snapshot.is_outlier:
        reasons.append(
            f"spread dislocation {latest_spread:.4f} ({_format_zscore(spread_snapshot.zscore)}z)"
        )

    return tuple(reasons)


def _signal_severity(
    *,
    return_snapshot: MetricSnapshot,
    volume_snapshot: MetricSnapshot,
    spread_snapshot: MetricSnapshot,
) -> float:
    """Use the strongest triggered metric as severity."""
    severity = 0.0
    for snapshot in (return_snapshot, volume_snapshot, spread_snapshot):
        if snapshot.is_outlier:
            severity = max(severity, snapshot.abs_zscore)
    return severity


def _returns(prices: Sequence[float]) -> list[float]:
    """Return simple decimal returns between consecutive prices."""
    returns: list[float] = []
    for previous, current in zip(prices, prices[1:]):
        if previous <= 0:
            continue
        returns.append((current - previous) / previous)
    return returns


def _mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean for a non-empty sequence."""
    if not values:
        return 0.0
    return math.fsum(values) / len(values)


def _population_stddev(values: Sequence[float]) -> float:
    """Return the population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
    return variance**0.5


def _zscore(value: float, *, mean: float, stddev: float, sample_size: int) -> float:
    """Return a z-score against a baseline, handling zero-variance windows."""
    if sample_size < 2:
        return 0.0

    if stddev <= 1e-12:
        delta = value - mean
        if abs(delta) <= 1e-12:
            return 0.0
        return math.copysign(float("inf"), delta)

    return (value - mean) / stddev


def _format_zscore(value: float) -> str:
    """Format z-scores, including infinite values."""
    if math.isinf(value):
        return "inf"
    return f"{value:.2f}"


def _read_first_float(
    payload: Any,
    field_names: Sequence[str],
    *,
    default: float,
) -> float:
    """Read the first finite float among candidate fields."""
    for field_name in field_names:
        value = _as_float(_read_field(payload, field_name), float("nan"))
        if math.isfinite(value):
            return value
    return default


def _read_field(payload: Any, key: str, default: Any = None) -> Any:
    """Read a field from a mapping or object."""
    if isinstance(payload, Mapping):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Convert arbitrary values to finite floats."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _as_timestamp(value: Any) -> float | None:
    """Convert optional timestamps to floats."""
    number = _as_float(value, float("nan"))
    if not math.isfinite(number):
        return None
    return number


def _as_optional_text(value: Any) -> str | None:
    """Convert non-empty values to text."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "AnomalyConfig",
    "AnomalyDetector",
    "AnomalyKind",
    "AnomalySignal",
    "DEFAULT_ANOMALY_CONFIG",
    "MetricSnapshot",
    "detect_anomaly",
    "detect_anomaly_from_history",
]
