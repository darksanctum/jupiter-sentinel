import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.strategies.arbitrage as arbitrage
from src.strategies.arbitrage import (
    DEFAULT_TRIANGLES,
    TriangleEvaluation,
    TriangleQuote,
    TriangularArbitrageScanner,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


def install_urlopen(monkeypatch, *responses):
    queue = list(responses)
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append((request, timeout))
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return FakeResponse(response)

    monkeypatch.setattr(arbitrage.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_get_quote_builds_request_and_parses_route_metadata(monkeypatch):
    calls = install_urlopen(
        monkeypatch,
        {
            "outAmount": "240",
            "priceImpactPct": "0.12",
            "contextSlot": 123,
            "platformFee": {"amount": "12"},
            "routePlan": [
                {"swapInfo": {"label": "Orca"}},
                {"swapInfo": {"label": "Orca"}},
                {"swapInfo": {"label": "Phoenix"}},
            ],
        },
    )
    scanner = TriangularArbitrageScanner(slippage_bps=75, quote_timeout=9)

    quote = scanner.get_quote(arbitrage.SOL_MINT, arbitrage.USDC_MINT, 120)

    assert quote is not None
    assert quote.input_amount == 120
    assert quote.out_amount == 240
    assert quote.route_labels == ("Orca", "Phoenix")
    assert quote.route_signature == "Orca -> Phoenix"
    assert quote.price_impact_pct == pytest.approx(0.12)
    assert quote.context_slot == 123
    assert quote.platform_fee_amount == 12
    assert quote.platform_fee_mint == arbitrage.USDC_MINT

    request, timeout = calls[0]
    assert timeout == 9
    assert request.full_url == (
        f"{arbitrage.JUPITER_SWAP_V1}/quote?"
        f"inputMint={arbitrage.SOL_MINT}&"
        f"outputMint={arbitrage.USDC_MINT}&"
        f"amount=120&"
        f"slippageBps=75&"
        f"onlyDirectRoutes=false"
    )
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"] == arbitrage.HEADERS["User-Agent"]
    assert headers["content-type"] == arbitrage.HEADERS["Content-Type"]
    if "x-api-key" in arbitrage.HEADERS:
        assert headers["x-api-key"] == arbitrage.HEADERS["x-api-key"]


def test_evaluate_triangle_flags_profitable_loop_after_fee_and_gas(monkeypatch):
    install_urlopen(
        monkeypatch,
        {
            "outAmount": "150000000",
            "platformFee": {"amount": "200000"},
            "routePlan": [{"swapInfo": {"label": "Meteora"}}],
        },
        {
            "outAmount": "300000000",
            "routePlan": [{"swapInfo": {"label": "Orca"}}],
        },
        {
            "outAmount": "1008000000",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}],
        },
        {
            "outAmount": "150000000",
            "platformFee": {"amount": "200000"},
            "routePlan": [{"swapInfo": {"label": "Meteora"}}],
        },
        {
            "outAmount": "300000000",
            "routePlan": [{"swapInfo": {"label": "Orca"}}],
        },
        {
            "outAmount": "1008000000",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}],
        },
    )
    scanner = TriangularArbitrageScanner(
        min_net_profit_pct=0.5,
        gas_cost_lamports_per_swap=5_000,
    )

    evaluation = scanner.evaluate_triangle(
        DEFAULT_TRIANGLES[0],
        starting_amount=1_000_000_000,
    )

    assert evaluation is not None
    assert evaluation.is_opportunity is True
    assert evaluation.path_name == "SOL -> USDC -> JUP -> SOL"
    assert evaluation.gross_profit_amount == 8_000_000
    assert evaluation.jupiter_fee_amount == 1_333_333
    assert evaluation.gas_cost_amount == 15_000
    assert evaluation.net_profit_amount == 6_651_667
    assert evaluation.gross_profit_pct == pytest.approx(0.8)
    assert evaluation.net_profit_pct == pytest.approx(0.6651667)
    assert [leg.route_signature for leg in evaluation.legs] == [
        "Meteora",
        "Orca",
        "Raydium",
    ]

    scanned = scanner.scan_triangle(
        DEFAULT_TRIANGLES[0],
        starting_amount=1_000_000_000,
    )
    assert scanned is not None
    assert scanner.opportunities == [scanned]


def test_scan_triangle_filters_loops_that_fall_below_net_threshold(monkeypatch):
    install_urlopen(
        monkeypatch,
        {
            "outAmount": "150000000",
            "platformFee": {"amount": "200000"},
            "routePlan": [{"swapInfo": {"label": "Meteora"}}],
        },
        {
            "outAmount": "300000000",
            "routePlan": [{"swapInfo": {"label": "Orca"}}],
        },
        {
            "outAmount": "1006000000",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}],
        },
        {
            "outAmount": "150000000",
            "platformFee": {"amount": "200000"},
            "routePlan": [{"swapInfo": {"label": "Meteora"}}],
        },
        {
            "outAmount": "300000000",
            "routePlan": [{"swapInfo": {"label": "Orca"}}],
        },
        {
            "outAmount": "1006000000",
            "routePlan": [{"swapInfo": {"label": "Raydium"}}],
        },
    )
    scanner = TriangularArbitrageScanner(
        min_net_profit_pct=0.5,
        gas_cost_lamports_per_swap=5_000,
    )

    evaluation = scanner.evaluate_triangle(
        DEFAULT_TRIANGLES[0],
        starting_amount=1_000_000_000,
    )

    assert evaluation is not None
    assert evaluation.is_opportunity is False
    assert evaluation.gross_profit_pct == pytest.approx(0.6)
    assert evaluation.net_profit_pct == pytest.approx(0.4651667)
    assert scanner.scan_triangle(
        DEFAULT_TRIANGLES[0],
        starting_amount=1_000_000_000,
    ) is None


def test_scan_all_flattens_profitable_triangles_into_report(monkeypatch):
    scanner = TriangularArbitrageScanner()
    profitable = TriangleEvaluation(
        path=DEFAULT_TRIANGLES[0],
        path_name="SOL -> USDC -> JUP -> SOL",
        starting_amount=1_000_000_000,
        ending_amount=1_008_000_000,
        start_mint=arbitrage.SOL_MINT,
        start_symbol="SOL",
        start_decimals=9,
        gross_profit_amount=8_000_000,
        jupiter_fee_amount=1_333_333,
        gas_cost_amount=15_000,
        net_profit_amount=6_651_667,
        gross_profit_pct=0.8,
        net_profit_pct=0.6651667,
        min_profit_pct=0.5,
        legs=(
            TriangleQuote(
                input_mint=arbitrage.SOL_MINT,
                output_mint=arbitrage.USDC_MINT,
                input_amount=1_000_000_000,
                out_amount=150_000_000,
                input_decimals=9,
                output_decimals=6,
                route_labels=("Meteora",),
                price_impact_pct=0.0,
            ),
            TriangleQuote(
                input_mint=arbitrage.USDC_MINT,
                output_mint=arbitrage.JUP_MINT,
                input_amount=150_000_000,
                out_amount=300_000_000,
                input_decimals=6,
                output_decimals=6,
                route_labels=("Orca",),
                price_impact_pct=0.0,
            ),
            TriangleQuote(
                input_mint=arbitrage.JUP_MINT,
                output_mint=arbitrage.SOL_MINT,
                input_amount=300_000_000,
                out_amount=1_008_000_000,
                input_decimals=6,
                output_decimals=9,
                route_labels=("Raydium",),
                price_impact_pct=0.0,
            ),
        ),
    )
    monkeypatch.setattr(
        scanner,
        "scan_triangle",
        lambda triangle, starting_amount=None: profitable
        if tuple(triangle) == DEFAULT_TRIANGLES[0]
        else None,
    )

    report = scanner.scan_all([DEFAULT_TRIANGLES[0]], starting_amount=1_000_000_000)

    assert report["triangles_scanned"] == 1
    assert len(report["opportunities"]) == 1
    assert report["opportunities"][0]["path"] == "SOL -> USDC -> JUP -> SOL"
    assert report["opportunities"][0]["net_profit_pct"] == pytest.approx(0.6651667)
    assert report["opportunities"][0]["opportunity"] is True
    assert isinstance(report["timestamp"], str)
