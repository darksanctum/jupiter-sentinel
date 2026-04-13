from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.chain.ethereum import (
    DEFAULT_APPROVAL_GAS_LIMIT,
    DEFAULT_GAS_LIMITS,
    EthereumChain,
    evaluate_trade_profitability,
    is_trade_profitable,
)


def test_supports_usdc_usdt_weth_symbols_and_addresses():
    chain = EthereumChain(eth_usd_spot_url="")

    assert chain.supports_token("USDC") is True
    assert chain.supports_token("usdt") is True
    assert (
        chain.token("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48").symbol == "USDC"
    )
    assert (
        chain.token("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2").symbol == "WETH"
    )

    assert chain.supports_pair("USDC", "WETH") is True
    assert chain.supports_pair("WETH", "USDT") is True
    assert chain.supports_pair("USDC", "USDC") is False
    assert chain.supports_pair("USDC", "DAI") is False


def test_estimate_gas_cost_uses_pair_defaults_and_optional_approval():
    chain = EthereumChain(eth_usd_spot_url="")

    estimate = chain.estimate_gas_cost(
        "USDC",
        "WETH",
        gas_price_wei=30_000_000_000,
        eth_price_usd=3_500,
        include_approval=True,
    )

    expected_limit = DEFAULT_GAS_LIMITS[("USDC", "WETH")] + DEFAULT_APPROVAL_GAS_LIMIT
    assert estimate.gas_limit == expected_limit
    assert estimate.gas_cost_wei == expected_limit * 30_000_000_000
    assert estimate.gas_cost_eth == pytest.approx(0.00615)
    assert estimate.gas_cost_usd == pytest.approx(21.525)
    assert estimate.includes_approval is True


def test_evaluate_trade_flags_unprofitable_after_l1_gas():
    chain = EthereumChain(min_profit_usd=0.0, eth_usd_spot_url="")

    result = chain.evaluate_trade(
        "USDC",
        "USDT",
        100_000_000,
        101_000_000,
        gas_price_wei=20_000_000_000,
        eth_price_usd=2_000,
    )

    assert result.gross_profit_usd == pytest.approx(1.0)
    assert result.gas_estimate.gas_cost_usd == pytest.approx(5.0)
    assert result.net_profit_usd == pytest.approx(-4.0)
    assert result.profitable is False
    assert result.break_even_output_amount_units == pytest.approx(105.0)


def test_evaluate_trade_handles_weth_pricing_and_positive_net_profit():
    chain = EthereumChain(min_profit_usd=1.0, eth_usd_spot_url="")

    result = chain.evaluate_trade(
        "WETH",
        "USDC",
        50_000_000_000_000_000,
        210_000_000,
        gas_price_wei=5_000_000_000,
        eth_price_usd=4_000,
    )

    assert result.input_value_usd == pytest.approx(200.0)
    assert result.output_value_usd == pytest.approx(210.0)
    assert result.gross_profit_usd == pytest.approx(10.0)
    assert result.net_profit_usd == pytest.approx(6.8)
    assert result.profitable is True
    assert result.net_profit_pct == pytest.approx(3.4)


def test_module_level_helpers_match_chain_behavior():
    result = evaluate_trade_profitability(
        "USDT",
        "USDC",
        100_000_000,
        107_000_000,
        gas_price_wei=20_000_000_000,
        eth_price_usd=2_000,
    )

    assert result.profitable is True
    assert is_trade_profitable(
        "USDT",
        "USDC",
        100_000_000,
        107_000_000,
        gas_price_wei=20_000_000_000,
        eth_price_usd=2_000,
    ) is True
