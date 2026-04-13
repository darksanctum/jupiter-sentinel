import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import JUP_MINT, USDC_MINT
from src.oracle import PriceFeed, PricePoint
from src.strategies.momentum import momentum_score, scan_for_signals


def make_feed(pair_name, prices):
    feed = PriceFeed(pair_name=pair_name, input_mint=JUP_MINT, output_mint=USDC_MINT)
    for index, price in enumerate(prices):
        feed.history.append(PricePoint(timestamp=float(1_700_000_000 + index), price=float(price)))
    return feed


def test_momentum_score_returns_zero_without_qualifying_trailing_streak():
    assert momentum_score([100.0, 100.4, 100.8, 101.0], min_increase_pct=0.5) == 0.0


def test_momentum_score_validates_threshold():
    with pytest.raises(ValueError, match="min_increase_pct must be a finite percentage >= 0"):
        momentum_score([100.0, 101.0], min_increase_pct=-0.5)


def test_scan_for_signals_returns_scale_in_buy_signal_for_minimum_streak():
    signal = scan_for_signals([make_feed("JUP/USDC", [100.0, 101.0, 102.5, 104.2])])[0]

    assert signal["strategy"] == "momentum"
    assert signal["pair"] == "JUP/USDC"
    assert signal["direction"] == "UP"
    assert signal["action"] == "BUY"
    assert signal["side"] == "LONG"
    assert signal["reason"] == "consecutive_price_increases"
    assert signal["consecutive_increases"] == 3
    assert signal["price"] == pytest.approx(104.2)
    assert signal["starting_price"] == pytest.approx(100.0)
    assert signal["average_increase_pct"] == pytest.approx(1.3812285879091842)
    assert signal["cumulative_change_pct"] == pytest.approx(4.2)
    assert signal["momentum_score"] == pytest.approx(4.143685763727553)
    assert signal["entry_mode"] == "SCALE_IN"
    assert signal["scale_step"] == 1
    assert signal["scale_steps_total"] == 4
    assert signal["incremental_allocation_fraction"] == pytest.approx(0.25)
    assert signal["allocation_fraction"] == pytest.approx(0.25)
    assert signal["data_points"] == 4


def test_scan_for_signals_scales_deeper_for_longer_streaks_and_sorts_by_score():
    stronger = make_feed("BONK/USDC", [100.0, 101.0, 102.5, 104.2, 106.4, 109.1])
    weaker = make_feed("WIF/USDC", [100.0, 100.7, 101.3, 102.0])

    signals = scan_for_signals([weaker, stronger])

    assert [signal["pair"] for signal in signals] == ["BONK/USDC", "WIF/USDC"]
    assert signals[0]["consecutive_increases"] == 5
    assert signals[0]["scale_step"] == 3
    assert signals[0]["allocation_fraction"] == pytest.approx(0.75)
    assert signals[0]["momentum_score"] > signals[1]["momentum_score"]


def test_scan_for_signals_skips_histories_without_a_qualifying_streak():
    insufficient_history = make_feed("JUP/USDC", [100.0, 101.0, 102.0])
    broken_streak = make_feed("SOL/USDC", [100.0, 101.0, 100.8, 101.6, 102.5])

    assert scan_for_signals([insufficient_history, broken_streak], min_increase_pct=0.75) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_consecutive_increases": 0}, "min_consecutive_increases must be at least 1"),
        ({"min_increase_pct": -1}, "min_increase_pct must be a finite percentage >= 0"),
        ({"max_scale_steps": 0}, "max_scale_steps must be at least 1"),
    ],
)
def test_scan_for_signals_validates_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        scan_for_signals([], **kwargs)
