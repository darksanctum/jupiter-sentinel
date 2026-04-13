import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import JUP_MINT, USDC_MINT
from src.oracle import PriceFeed, PricePoint
from src.strategies.mean_reversion import scan_for_signals


def make_feed(pair_name, prices):
    feed = PriceFeed(pair_name=pair_name, input_mint=JUP_MINT, output_mint=USDC_MINT)
    for index, price in enumerate(prices):
        feed.history.append(PricePoint(timestamp=float(1_700_000_000 + index), price=float(price)))
    return feed


def test_scan_for_signals_returns_long_signal_when_price_breaks_lower_band():
    signal = scan_for_signals([make_feed("JUP/USDC", [100.0] * 19 + [90.0])])[0]

    assert signal["strategy"] == "mean_reversion"
    assert signal["pair"] == "JUP/USDC"
    assert signal["direction"] == "DOWN"
    assert signal["action"] == "BUY"
    assert signal["side"] == "LONG"
    assert signal["reason"] == "price_below_lower_band"
    assert signal["price"] == pytest.approx(90.0)
    assert signal["moving_average"] == pytest.approx(99.5)
    assert signal["lower_band"] == pytest.approx(95.14110105645933)
    assert signal["upper_band"] == pytest.approx(103.85889894354067)
    assert signal["z_score"] == pytest.approx(-4.358898943540674)
    assert signal["deviation_pct"] == pytest.approx(-9.547738693467336)
    assert signal["target_price"] == pytest.approx(signal["moving_average"])
    assert signal["window"] == 20
    assert signal["data_points"] == 20


def test_scan_for_signals_returns_short_signal_when_price_breaks_upper_band():
    signal = scan_for_signals([make_feed("BONK/USDC", [100.0] * 19 + [110.0])])[0]

    assert signal["pair"] == "BONK/USDC"
    assert signal["direction"] == "UP"
    assert signal["action"] == "SELL"
    assert signal["side"] == "SHORT"
    assert signal["reason"] == "price_above_upper_band"
    assert signal["price"] == pytest.approx(110.0)
    assert signal["moving_average"] == pytest.approx(100.5)
    assert signal["target_price"] == pytest.approx(100.5)
    assert signal["z_score"] == pytest.approx(4.358898943540674)


def test_scan_for_signals_skips_feeds_without_enough_history_or_bandwidth():
    insufficient_history = make_feed("JUP/USDC", [100.0] * 19)
    flat_history = make_feed("SOL/USDC", [100.0] * 20)

    assert scan_for_signals([insufficient_history, flat_history]) == []


def test_scan_for_signals_ignores_prices_inside_bands_and_honors_min_bandwidth():
    inside_bands = make_feed("WIF/USDC", [100.0, 101.0] * 10)
    lower_break = make_feed("JUP/SOL", [100.0] * 19 + [90.0])

    assert scan_for_signals([inside_bands]) == []
    assert scan_for_signals([lower_break], min_bandwidth_pct=9.0) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"window": 1}, "window must be at least 2"),
        ({"stddev_multiplier": 0}, "stddev_multiplier must be a positive finite number"),
        ({"min_bandwidth_pct": -1}, "min_bandwidth_pct must be a finite percentage >= 0"),
    ],
)
def test_scan_for_signals_validates_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        scan_for_signals([], **kwargs)
