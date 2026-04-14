import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.strategies.arbitrage as arbitrage_module
from src.config import JUP_MINT, SOL_MINT, USDC_MINT
from src.oracle import PriceFeed, PricePoint
from src.strategies.arbitrage import (
    DEFAULT_TRIANGLES,
    TriangleQuote,
    TriangularArbitrageScanner,
)
from src.strategies.mean_reversion import scan_for_signals as scan_mean_reversion_signals
from src.strategies.momentum import (
    momentum_score,
    scan_for_signals as scan_momentum_signals,
)
from src.strategies.smart_dca import simulate_smart_dca


def make_feed(
    pair_name,
    prices,
    *,
    input_mint=JUP_MINT,
    output_mint=USDC_MINT,
):
    feed = PriceFeed(pair_name=pair_name, input_mint=input_mint, output_mint=output_mint)
    for index, price in enumerate(prices):
        feed.history.append(
            PricePoint(
                timestamp=float(1_700_000_000 + index),
                price=float(price),
            )
        )
    return feed


def make_triangle_quote(
    input_mint,
    output_mint,
    input_amount,
    out_amount,
    *,
    route_labels=("MockDEX",),
    platform_fee_amount=0,
    platform_fee_mint="",
):
    return TriangleQuote(
        input_mint=input_mint,
        output_mint=output_mint,
        input_amount=input_amount,
        out_amount=out_amount,
        input_decimals=arbitrage_module.KNOWN_TOKEN_DECIMALS[input_mint],
        output_decimals=arbitrage_module.KNOWN_TOKEN_DECIMALS[output_mint],
        route_labels=route_labels,
        price_impact_pct=0.0,
        platform_fee_amount=platform_fee_amount,
        platform_fee_mint=platform_fee_mint,
        platform_fee_decimals=arbitrage_module.KNOWN_TOKEN_DECIMALS.get(
            platform_fee_mint or output_mint
        ),
    )


def install_quote_lookup(monkeypatch, scanner, quote_map):
    def fake_get_quote(input_mint, output_mint, amount):
        return quote_map.get((input_mint, output_mint, amount))

    monkeypatch.setattr(scanner, "get_quote", fake_get_quote)


@pytest.mark.parametrize(
    ("prices", "expected_direction", "expected_action", "expected_side", "expected_reason"),
    [
        ([100.0] * 19 + [90.0], "DOWN", "BUY", "LONG", "price_below_lower_band"),
        ([100.0] * 19 + [110.0], "UP", "SELL", "SHORT", "price_above_upper_band"),
    ],
)
def test_mean_reversion_emits_entry_and_exit_signals_for_band_breaks(
    prices,
    expected_direction,
    expected_action,
    expected_side,
    expected_reason,
):
    signal = scan_mean_reversion_signals([make_feed("JUP/USDC", prices)])[0]

    assert signal["strategy"] == "mean_reversion"
    assert signal["pair"] == "JUP/USDC"
    assert signal["direction"] == expected_direction
    assert signal["action"] == expected_action
    assert signal["side"] == expected_side
    assert signal["reason"] == expected_reason
    assert signal["target_price"] == pytest.approx(signal["moving_average"])


def test_mean_reversion_skips_empty_zero_and_inside_band_histories():
    assert scan_mean_reversion_signals([]) == []
    assert scan_mean_reversion_signals([make_feed("ZERO/USDC", [0.0] * 20)]) == []
    assert (
        scan_mean_reversion_signals([make_feed("INSIDE/USDC", [100.0, 101.0] * 10)]) == []
    )


def test_mean_reversion_handles_extreme_but_finite_prices():
    signal = scan_mean_reversion_signals(
        [make_feed("MEGA/USDC", [1_000_000_000_000.0] * 19 + [900_000_000_000.0])]
    )[0]

    assert signal["action"] == "BUY"
    assert signal["reason"] == "price_below_lower_band"
    for key in ("moving_average", "lower_band", "upper_band", "z_score", "deviation_pct"):
        assert math.isfinite(signal[key])


def test_momentum_generates_long_signal_for_known_uptrend():
    signal = scan_momentum_signals([make_feed("JUP/USDC", [100.0, 101.0, 102.5, 104.2])])[0]

    assert signal["strategy"] == "momentum"
    assert signal["pair"] == "JUP/USDC"
    assert signal["action"] == "BUY"
    assert signal["side"] == "LONG"
    assert signal["reason"] == "consecutive_price_increases"
    assert signal["consecutive_increases"] == 3
    assert signal["entry_mode"] == "SCALE_IN"
    assert signal["scale_step"] == 1
    assert signal["momentum_score"] > 0


def test_momentum_does_not_enter_after_trailing_pullback_or_zero_prices():
    broken_streak = make_feed("SOL/USDC", [100.0, 101.0, 102.0, 101.7])
    zero_prices = make_feed("ZERO/USDC", [0.0, 0.0, 0.0, 0.0])

    assert momentum_score([100.0, 101.0, 102.0, 101.7]) == 0.0
    assert scan_momentum_signals([broken_streak, zero_prices]) == []


def test_momentum_handles_empty_and_extreme_series():
    extreme_prices = [1_000_000_000_000.0, 1_010_000_000_000.0, 1_020_100_000_000.0, 1_040_502_000_000.0]

    assert momentum_score([]) == 0.0
    assert scan_momentum_signals([]) == []

    signal = scan_momentum_signals([make_feed("MEGA/USDC", extreme_prices)])[0]
    assert signal["momentum_score"] == pytest.approx(momentum_score(extreme_prices))
    assert math.isfinite(signal["average_increase_pct"])
    assert math.isfinite(signal["cumulative_change_pct"])


@pytest.mark.parametrize(
    ("prices", "expected_reason", "expected_amount", "expected_multiplier"),
    [
        ([100.0] * 19 + [90.0], "price_below_lower_band", 2.0, 2.0),
        ([100.0] * 19 + [110.0], "price_above_upper_band", 0.5, 0.5),
    ],
)
def test_smart_dca_adjusts_last_entry_for_bollinger_breaks(
    prices,
    expected_reason,
    expected_amount,
    expected_multiplier,
):
    state = simulate_smart_dca(
        make_feed("JUP/USDC", prices),
        base_amount=1.0,
        multiplier=2.0,
    )
    last_entry = state.entries[-1]

    assert last_entry.reason == expected_reason
    assert last_entry.amount == pytest.approx(expected_amount)
    assert last_entry.allocation_multiplier == pytest.approx(expected_multiplier)
    assert last_entry.accumulated_position == pytest.approx(state.accumulated_position)
    assert last_entry.average_entry_price == pytest.approx(state.average_entry_price)


def test_smart_dca_uses_base_entries_until_window_is_full():
    state = simulate_smart_dca(
        make_feed("JUP/USDC", [100.0, 101.0, 102.0]),
        base_amount=1.5,
        window=5,
    )

    assert len(state.entries) == 3
    assert [entry.amount for entry in state.entries] == pytest.approx([1.5, 1.5, 1.5])
    assert [entry.reason for entry in state.entries] == ["insufficient_history"] * 3
    assert state.total_invested == pytest.approx(4.5)
    assert state.average_entry_price > 0


def test_smart_dca_handles_empty_zero_and_extreme_histories():
    empty_state = simulate_smart_dca([], base_amount=1.0)
    assert empty_state.entries == []
    assert empty_state.total_invested == 0.0
    assert empty_state.accumulated_position == 0.0

    with pytest.raises(ValueError, match="prices must be positive finite numbers"):
        simulate_smart_dca([0.0, 0.0, 0.0], base_amount=1.0)

    extreme_state = simulate_smart_dca(
        make_feed("MEGA/USDC", [1_000_000_000_000.0] * 19 + [800_000_000_000.0]),
        base_amount=10.0,
        multiplier=3.0,
    )
    last_entry = extreme_state.entries[-1]
    assert last_entry.reason == "price_below_lower_band"
    assert last_entry.amount == pytest.approx(30.0)
    assert math.isfinite(extreme_state.average_entry_price)


def test_arbitrage_identifies_profitable_triangle_and_records_opportunity(monkeypatch):
    starting_amount = 1_000_000_000
    scanner = TriangularArbitrageScanner(
        min_net_profit_pct=0.5,
        gas_cost_lamports_per_swap=5_000,
    )
    quote_map = {
        (SOL_MINT, USDC_MINT, starting_amount): make_triangle_quote(
            SOL_MINT,
            USDC_MINT,
            starting_amount,
            150_000_000,
            route_labels=("Meteora",),
            platform_fee_amount=200_000,
            platform_fee_mint=USDC_MINT,
        ),
        (USDC_MINT, JUP_MINT, 150_000_000): make_triangle_quote(
            USDC_MINT,
            JUP_MINT,
            150_000_000,
            300_000_000,
            route_labels=("Orca",),
        ),
        (JUP_MINT, SOL_MINT, 300_000_000): make_triangle_quote(
            JUP_MINT,
            SOL_MINT,
            300_000_000,
            1_008_000_000,
            route_labels=("Raydium",),
        ),
    }
    install_quote_lookup(monkeypatch, scanner, quote_map)

    evaluation = scanner.scan_triangle(DEFAULT_TRIANGLES[0], starting_amount=starting_amount)

    assert evaluation is not None
    assert evaluation.is_opportunity is True
    assert evaluation.path_name == "SOL -> USDC -> JUP -> SOL"
    assert evaluation.gross_profit_amount == 8_000_000
    assert evaluation.net_profit_amount == 6_651_667
    assert [leg.route_signature for leg in evaluation.legs] == ["Meteora", "Orca", "Raydium"]
    assert scanner.opportunities == [evaluation]


def test_arbitrage_filters_unprofitable_triangle_without_recording_it(monkeypatch):
    starting_amount = 1_000_000_000
    scanner = TriangularArbitrageScanner(
        min_net_profit_pct=0.5,
        gas_cost_lamports_per_swap=5_000,
    )
    quote_map = {
        (SOL_MINT, USDC_MINT, starting_amount): make_triangle_quote(
            SOL_MINT,
            USDC_MINT,
            starting_amount,
            150_000_000,
            route_labels=("Meteora",),
            platform_fee_amount=200_000,
            platform_fee_mint=USDC_MINT,
        ),
        (USDC_MINT, JUP_MINT, 150_000_000): make_triangle_quote(
            USDC_MINT,
            JUP_MINT,
            150_000_000,
            300_000_000,
            route_labels=("Orca",),
        ),
        (JUP_MINT, SOL_MINT, 300_000_000): make_triangle_quote(
            JUP_MINT,
            SOL_MINT,
            300_000_000,
            1_006_000_000,
            route_labels=("Raydium",),
        ),
    }
    install_quote_lookup(monkeypatch, scanner, quote_map)

    evaluation = scanner.evaluate_triangle(DEFAULT_TRIANGLES[0], starting_amount=starting_amount)

    assert evaluation is not None
    assert evaluation.is_opportunity is False
    assert scanner.scan_triangle(DEFAULT_TRIANGLES[0], starting_amount=starting_amount) is None
    assert scanner.opportunities == []


def test_arbitrage_handles_missing_zero_and_extreme_inputs(monkeypatch):
    scanner = TriangularArbitrageScanner(gas_cost_lamports_per_swap=0)

    assert scanner.get_quote(SOL_MINT, USDC_MINT, 0) is None

    monkeypatch.setattr(scanner, "get_quote", lambda input_mint, output_mint, amount: None)
    assert scanner.evaluate_triangle(DEFAULT_TRIANGLES[0], starting_amount=1_000_000_000) is None

    extreme_start_amount = 1_000_000_000_000_000_000
    extreme_scanner = TriangularArbitrageScanner(gas_cost_lamports_per_swap=0)
    extreme_quote_map = {
        (SOL_MINT, USDC_MINT, extreme_start_amount): make_triangle_quote(
            SOL_MINT,
            USDC_MINT,
            extreme_start_amount,
            150_000_000_000_000,
        ),
        (USDC_MINT, JUP_MINT, 150_000_000_000_000): make_triangle_quote(
            USDC_MINT,
            JUP_MINT,
            150_000_000_000_000,
            300_000_000_000_000,
        ),
        (JUP_MINT, SOL_MINT, 300_000_000_000_000): make_triangle_quote(
            JUP_MINT,
            SOL_MINT,
            300_000_000_000_000,
            1_050_000_000_000_000_000,
        ),
    }
    install_quote_lookup(monkeypatch, extreme_scanner, extreme_quote_map)

    evaluation = extreme_scanner.evaluate_triangle(
        DEFAULT_TRIANGLES[0],
        starting_amount=extreme_start_amount,
    )

    assert evaluation is not None
    assert evaluation.net_profit_amount > 0
    assert evaluation.ending_amount == 1_050_000_000_000_000_000
