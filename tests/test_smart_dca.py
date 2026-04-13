import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

fake_state_manager = types.ModuleType("src.state_manager")


class FakeStateManager:
    def __init__(self, *args, **kwargs):
        pass


fake_state_manager.DEFAULT_LOCK_PCT = 0.5
fake_state_manager.LOCK_PCT_ENV = "LOCK_PCT"
fake_state_manager.StateManager = FakeStateManager
sys.modules.setdefault("src.state_manager", fake_state_manager)

from src.config import JUP_MINT, USDC_MINT
from src.oracle import PriceFeed, PricePoint
from src.strategies.smart_dca import simulate_smart_dca


def make_feed(pair_name, prices):
    feed = PriceFeed(pair_name=pair_name, input_mint=JUP_MINT, output_mint=USDC_MINT)
    for index, price in enumerate(prices):
        feed.history.append(PricePoint(timestamp=float(1_700_000_000 + index), price=float(price)))
    return feed


def test_simulate_smart_dca_uses_base_amount_until_window_is_full():
    state = simulate_smart_dca(make_feed("JUP/USDC", [100.0, 101.0, 102.0]), base_amount=1.5, window=5)

    assert len(state.entries) == 3
    assert [entry.amount for entry in state.entries] == pytest.approx([1.5, 1.5, 1.5])
    assert [entry.reason for entry in state.entries] == ["insufficient_history"] * 3

    expected_position = (1.5 / 100.0) + (1.5 / 101.0) + (1.5 / 102.0)
    assert state.total_invested == pytest.approx(4.5)
    assert state.accumulated_position == pytest.approx(expected_position)
    assert state.average_entry_price == pytest.approx(4.5 / expected_position)


def test_simulate_smart_dca_scales_up_below_lower_band_and_updates_average_entry():
    state = simulate_smart_dca(make_feed("JUP/USDC", [100.0] * 19 + [90.0]), base_amount=1.0, multiplier=2.0)
    last_entry = state.entries[-1]

    expected_position = (19 * (1.0 / 100.0)) + (2.0 / 90.0)

    assert last_entry.reason == "price_below_lower_band"
    assert last_entry.amount == pytest.approx(2.0)
    assert last_entry.allocation_multiplier == pytest.approx(2.0)
    assert state.total_invested == pytest.approx(21.0)
    assert state.accumulated_position == pytest.approx(expected_position)
    assert last_entry.accumulated_position == pytest.approx(expected_position)
    assert state.average_entry_price == pytest.approx(21.0 / expected_position)
    assert last_entry.average_entry_price == pytest.approx(21.0 / expected_position)


def test_simulate_smart_dca_scales_down_above_upper_band():
    state = simulate_smart_dca(make_feed("BONK/USDC", [100.0] * 19 + [110.0]), base_amount=1.0, multiplier=2.0)
    last_entry = state.entries[-1]

    expected_position = (19 * (1.0 / 100.0)) + (0.5 / 110.0)

    assert last_entry.reason == "price_above_upper_band"
    assert last_entry.amount == pytest.approx(0.5)
    assert last_entry.allocation_multiplier == pytest.approx(0.5)
    assert state.total_invested == pytest.approx(19.5)
    assert state.accumulated_position == pytest.approx(expected_position)
    assert state.average_entry_price == pytest.approx(19.5 / expected_position)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_amount": 0}, "base_amount must be a positive finite number"),
        ({"multiplier": 0.5}, "multiplier must be a finite number >= 1"),
        ({"window": 1}, "window must be at least 2"),
        ({"stddev_multiplier": 0}, "stddev_multiplier must be a positive finite number"),
    ],
)
def test_simulate_smart_dca_validates_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        simulate_smart_dca(make_feed("JUP/USDC", [100.0, 101.0]), **kwargs)
