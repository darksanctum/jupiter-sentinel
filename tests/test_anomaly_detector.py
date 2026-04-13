from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ml.anomaly_detector import (
    AnomalyDetector,
    AnomalyKind,
    detect_anomaly,
)
from src.oracle import PriceFeed, PricePoint


@dataclass(frozen=True)
class MarketPoint:
    timestamp: float
    price: float
    volume_estimate: float
    spread_bps: float | None = None


def make_market_history(
    *,
    final_return: float,
    final_volume: float,
    final_spread_bps: float | None,
) -> list[MarketPoint]:
    price = 100.0
    history: list[MarketPoint] = []
    baseline_returns = [0.0010, -0.0008, 0.0012, -0.0009] * 8
    baseline_volumes = [96_000.0, 101_000.0, 99_500.0, 103_000.0]
    baseline_spreads = [11.0, 13.0, 12.0, 14.0]

    for index, step_return in enumerate(baseline_returns):
        price *= 1.0 + step_return
        history.append(
            MarketPoint(
                timestamp=float(index),
                price=price,
                volume_estimate=baseline_volumes[index % len(baseline_volumes)],
                spread_bps=baseline_spreads[index % len(baseline_spreads)],
            )
        )

    history.append(
        MarketPoint(
            timestamp=float(len(history)),
            price=price * (1.0 + final_return),
            volume_estimate=final_volume,
            spread_bps=final_spread_bps,
        )
    )
    return history


def make_feed_history(*, final_return: float, final_volume: float) -> PriceFeed:
    price = 100.0
    feed = PriceFeed("TEST/USDC", "INPUT", "OUTPUT")
    baseline_returns = [0.0010, -0.0008, 0.0012, -0.0009] * 8
    baseline_volumes = [96_000.0, 101_000.0, 99_500.0, 103_000.0]

    for index, step_return in enumerate(baseline_returns):
        price *= 1.0 + step_return
        feed.history.append(
            PricePoint(
                timestamp=float(index),
                price=price,
                volume_estimate=baseline_volumes[index % len(baseline_volumes)],
            )
        )

    feed.history.append(
        PricePoint(
            timestamp=float(len(feed.history)),
            price=price * (1.0 + final_return),
            volume_estimate=final_volume,
        )
    )
    return feed


def test_detect_anomaly_flags_pump_conditions():
    history = make_market_history(
        final_return=0.12,
        final_volume=540_000.0,
        final_spread_bps=95.0,
    )

    signal = detect_anomaly(SimpleNamespace(pair_name="TEST/USDC", history=history))

    assert signal.flagged is True
    assert signal.kind is AnomalyKind.PUMP
    assert signal.triggered_metrics == ("returns", "volume", "spread")
    assert signal.latest_return > 0
    assert "upside return outlier" in signal.summary


def test_detect_anomaly_flags_dump_without_spread_data():
    feed = make_feed_history(final_return=-0.11, final_volume=510_000.0)

    signal = AnomalyDetector().detect(feed)

    assert signal.flagged is True
    assert signal.kind is AnomalyKind.DUMP
    assert signal.return_snapshot.is_outlier is True
    assert signal.volume_snapshot.is_outlier is True
    assert signal.spread_snapshot.is_outlier is False
    assert signal.latest_spread == 0.0


def test_detect_anomaly_flags_whale_manipulation_from_volume_and_spread():
    history = make_market_history(
        final_return=0.004,
        final_volume=620_000.0,
        final_spread_bps=125.0,
    )

    signal = detect_anomaly(SimpleNamespace(pair_name="TEST/USDC", history=history))

    assert signal.flagged is True
    assert signal.kind is AnomalyKind.WHALE_MANIPULATION
    assert signal.return_snapshot.is_outlier is False
    assert signal.volume_snapshot.is_outlier is True
    assert signal.spread_snapshot.is_outlier is True


def test_detect_anomaly_flags_market_event_on_price_shock_only():
    history = make_market_history(
        final_return=0.10,
        final_volume=102_000.0,
        final_spread_bps=13.0,
    )

    signal = detect_anomaly(SimpleNamespace(pair_name="TEST/USDC", history=history))

    assert signal.flagged is True
    assert signal.kind is AnomalyKind.MARKET_EVENT
    assert signal.return_snapshot.is_outlier is True
    assert signal.volume_snapshot.is_outlier is False
    assert signal.spread_snapshot.is_outlier is False


def test_detect_anomaly_returns_normal_for_insufficient_history():
    short_history = make_market_history(
        final_return=0.08,
        final_volume=400_000.0,
        final_spread_bps=80.0,
    )[:5]

    signal = detect_anomaly(
        SimpleNamespace(pair_name="TEST/USDC", history=short_history)
    )

    assert signal.flagged is False
    assert signal.kind is AnomalyKind.NORMAL
    assert signal.reasons == ("insufficient_history",)
