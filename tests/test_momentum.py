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


def test_momentum_score_scores_latest_trailing_streak():
    assert momentum_score([100.0, 101.0, 102.01, 103.0301]) == pytest.approx(71.18060000000004)
    assert momentum_score([100.0, 101.0, 102.01, 101.5]) == 0.0


def test_scan_for_signals_returns_scale_in_buy_signal_for_upward_streak():
    signal = scan_for_signals([make_feed("JUP/USDC", [100.0, 101.0, 102.01, 103.0301])])[0]

    assert signal["strategy"] == "momentum"
    assert signal["pair"] == "JUP/USDC"
    assert signal["direction"] == "UP"
    assert signal["action"] == "BUY"
    assert signal["side"] == "LONG"
    assert signal["reason"] == "consecutive_upward_momentum"
    assert signal["price"] == pytest.approx(103.0301)
    assert signal["momentum_score"] == pytest.approx(71.18060000000004)
    assert signal["consecutive_increases"] == 3
    assert signal["latest_change_pct"] == pytest.approx(1.0)
    assert signal["average_step_change_pct"] == pytest.approx(1.0)
    assert signal["cumulative_change_pct"] == pytest.approx(3.0301000000000045)
    assert signal["target_position_fraction"] == pytest.approx(0.6)
    assert signal["entry_style"] == "scale_in"
    assert signal["scale_plan"] == [
        {
            "stage": 1,
            "fraction_of_target_position": pytest.approx(0.5),
            "fraction_of_max_position": pytest.approx(0.3),
            "trigger": "enter_on_current_signal",
        },
        {
            "stage": 2,
            "fraction_of_target_position": pytest.approx(1 / 3),
            "fraction_of_max_position": pytest.approx(0.2),
            "trigger": "add_if_next_tick_gains_at_least_1.00%",
        },
        {
            "stage": 3,
            "fraction_of_target_position": pytest.approx(1 / 6),
            "fraction_of_max_position": pytest.approx(0.1),
            "trigger": "add_if_2_more_consecutive_ticks_gain_at_least_1.00%",
        },
    ]
    assert signal["data_points"] == 4


def test_scan_for_signals_skips_feeds_without_required_streak_or_with_invalid_prices():
    no_signal = make_feed("BONK/USDC", [100.0, 101.0, 102.01])
    invalid = make_feed("WIF/USDC", [100.0, 101.0, 0.0, 102.0])

    assert scan_for_signals([no_signal, invalid]) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_consecutive_increases": 2}, "min_consecutive_increases must be at least 3"),
        ({"min_step_change_pct": -1}, "min_step_change_pct must be a finite percentage >= 0"),
        ({"scale_stages": 1}, "scale_stages must be at least 2"),
    ],
)
def test_scan_for_signals_validates_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        scan_for_signals([], **kwargs)


def test_momentum_score_validates_threshold():
    with pytest.raises(ValueError, match="min_step_change_pct must be a finite percentage >= 0"):
        momentum_score([100.0, 101.0], min_step_change_pct=-0.5)
