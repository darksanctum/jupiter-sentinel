from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cross_chain_arbitrage import (
    CrossChainArbitrageDetector,
    CrossChainFeeSchedule,
    CrossChainPriceQuote,
    calculate_profitability,
    detect_arbitrage,
)


def test_calculate_profitability_flags_profitable_sol_between_solana_and_polygon():
    result = calculate_profitability(
        "SOL",
        150.0,
        153.5,
        evm_chain="polygon",
        trade_size_tokens=10.0,
        bridge_cost_usdc=4.0,
        gas_cost_usdc=1.5,
        source_swap_fee_bps=10.0,
        destination_swap_fee_bps=10.0,
        source_slippage_bps=5.0,
        destination_slippage_bps=5.0,
    )

    assert result.buy_quote.chain == "solana"
    assert result.sell_quote.chain == "polygon"
    assert result.gross_spread_usdc == pytest.approx(35.0)
    assert result.fees.total_usdc == pytest.approx(10.0525)
    assert result.net_profit_usdc == pytest.approx(24.9475)
    assert result.break_even_sell_price_usdc == pytest.approx(151.0015022534)
    assert result.profitable is True


def test_calculate_profitability_marks_opportunity_unprofitable_when_edge_is_too_small():
    result = calculate_profitability(
        "SOL",
        150.0,
        150.35,
        evm_chain="ethereum",
        trade_size_tokens=10.0,
        bridge_cost_usdc=1.5,
        gas_cost_usdc=2.2,
        source_swap_fee_bps=10.0,
        destination_swap_fee_bps=10.0,
    )

    assert result.gross_spread_usdc == pytest.approx(3.5)
    assert result.fees.total_usdc == pytest.approx(6.7035)
    assert result.net_profit_usdc == pytest.approx(-3.2035)
    assert result.profitable is False


def test_detector_returns_only_profitable_solana_vs_evm_opportunities_sorted_by_profit():
    fee_schedule = CrossChainFeeSchedule(bridge_cost_usdc=1.0, gas_cost_usdc=1.0)
    quotes = [
        CrossChainPriceQuote(chain="sol", token_symbol="SOL", price_usdc=150.0),
        CrossChainPriceQuote(
            chain="polygon",
            token_symbol="SOL",
            price_usdc=153.0,
            venue="QuickSwap",
        ),
        CrossChainPriceQuote(
            chain="eth",
            token_symbol="SOL",
            price_usdc=154.0,
            venue="Uniswap",
        ),
        CrossChainPriceQuote(
            chain="ethereum",
            token_symbol="SOL",
            price_usdc=155.0,
            quote_symbol="USDT",
        ),
        CrossChainPriceQuote(chain="polygon", token_symbol="BONK", price_usdc=0.00002),
    ]

    opportunities = detect_arbitrage(
        quotes,
        trade_size_tokens=10.0,
        fee_schedule=fee_schedule,
    )

    assert len(opportunities) == 2
    assert [opportunity.sell_quote.chain for opportunity in opportunities] == [
        "ethereum",
        "polygon",
    ]
    assert all(opportunity.buy_quote.chain == "solana" for opportunity in opportunities)
    assert opportunities[0].net_profit_usdc == pytest.approx(38.0)
    assert opportunities[1].net_profit_usdc == pytest.approx(28.0)


def test_break_even_sell_price_accounts_for_fixed_and_variable_fees():
    detector = CrossChainArbitrageDetector(min_profit_usdc=5.0)
    result = detector.evaluate_pair(
        CrossChainPriceQuote(chain="solana", token_symbol="SOL", price_usdc=100.0),
        CrossChainPriceQuote(chain="ethereum", token_symbol="SOL", price_usdc=101.5),
        trade_notional_usdc=1_000.0,
        fee_schedule=CrossChainFeeSchedule(
            bridge_cost_usdc=3.0,
            bridge_fee_bps=15.0,
            gas_cost_usdc=2.0,
            source_swap_fee_bps=10.0,
            destination_swap_fee_bps=20.0,
            other_fees_usdc=1.0,
        ),
    )

    assert result.trade_size_tokens == pytest.approx(10.0)
    assert result.fees.total_usdc == pytest.approx(10.53)
    assert result.net_profit_usdc == pytest.approx(4.47)
    assert result.break_even_sell_price_usdc == pytest.approx(101.5531062124)
    assert result.break_even_spread_usdc == pytest.approx(1.5531062124)
    assert result.profitable is False
