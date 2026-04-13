import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.cross_chain_arb as cross_chain_arb
from src.cross_chain_arb import (
    CrossChainArbDetector,
    RouteQuote,
    RouteSpreadOpportunity,
)

VALID_INPUT_MINT = cross_chain_arb.SCAN_PAIRS[0][0]
VALID_OUTPUT_MINT = cross_chain_arb.SCAN_PAIRS[0][1]


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

    monkeypatch.setattr(cross_chain_arb.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_get_quote_builds_request_and_parses_route_metadata(monkeypatch):
    calls = install_urlopen(
        monkeypatch,
        {
            "outAmount": "240",
            "priceImpactPct": "0.12",
            "contextSlot": 123,
            "routePlan": [
                {"swapInfo": {"label": "Orca"}},
                {"swapInfo": {"label": "Orca"}},
                {"swapInfo": {"label": "Phoenix"}},
            ],
        },
    )
    detector = CrossChainArbDetector(slippage_bps=75, quote_timeout=9)

    quote = detector.get_quote(VALID_INPUT_MINT, VALID_OUTPUT_MINT, 120)

    assert quote is not None
    assert quote.amount == 120
    assert quote.out_amount == 240
    assert quote.output_per_input == pytest.approx(2.0)
    assert quote.route_labels == ("Orca", "Phoenix")
    assert quote.route_signature == "Orca -> Phoenix"
    assert quote.price_impact_pct == pytest.approx(0.12)
    assert quote.context_slot == 123

    request, timeout = calls[0]
    assert timeout == 9
    assert request.full_url == (
        f"{cross_chain_arb.JUPITER_SWAP_V1}/quote?"
        f"inputMint={VALID_INPUT_MINT}&"
        f"outputMint={VALID_OUTPUT_MINT}&"
        f"amount=120&"
        f"slippageBps=75&"
        f"onlyDirectRoutes=false&"
        f"asLegacyTransaction=false"
    )
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"] == cross_chain_arb.HEADERS["User-Agent"]
    assert headers["content-type"] == cross_chain_arb.HEADERS["Content-Type"]
    if "x-api-key" in cross_chain_arb.HEADERS:
        assert headers["x-api-key"] == cross_chain_arb.HEADERS["x-api-key"]


@pytest.mark.parametrize(
    "response",
    [
        URLError("network down"),
        b"not-json",
        {},
        {"outAmount": "not-an-int"},
    ],
    ids=["network-error", "invalid-json", "missing-out-amount", "bad-out-amount"],
)
def test_get_quote_returns_none_on_invalid_quote_payload(monkeypatch, response):
    install_urlopen(monkeypatch, response)
    detector = CrossChainArbDetector()

    assert detector.get_quote(VALID_INPUT_MINT, VALID_OUTPUT_MINT, 100) is None


def test_scan_pair_surfaces_spreads_only_for_different_route_paths(monkeypatch):
    detector = CrossChainArbDetector(min_spread_pct=4.0)
    quotes = {
        100: RouteQuote(
            amount=100,
            out_amount=250,
            output_per_input=2.5,
            route_labels=("Meteora",),
            price_impact_pct=0.01,
        ),
        200: RouteQuote(
            amount=200,
            out_amount=480,
            output_per_input=2.4,
            route_labels=("Raydium",),
            price_impact_pct=0.02,
        ),
        300: RouteQuote(
            amount=300,
            out_amount=720,
            output_per_input=2.4,
            route_labels=("Raydium",),
            price_impact_pct=0.03,
        ),
        400: None,
    }
    monkeypatch.setattr(
        detector,
        "get_quote",
        lambda input_mint, output_mint, amount: quotes[amount],
    )

    opportunities = detector.scan_pair(
        "mint-in",
        "mint-out",
        "SOL/USDC",
        amounts=[100, 200, 300, 400],
    )

    assert len(opportunities) == 2
    assert detector.opportunities == opportunities
    assert all(opportunity.pair == "SOL/USDC" for opportunity in opportunities)
    assert all(opportunity.better_quote.route_signature == "Meteora" for opportunity in opportunities)
    assert all(opportunity.worse_quote.route_signature == "Raydium" for opportunity in opportunities)
    assert all(opportunity.spread_pct == pytest.approx((2.5 - 2.4) / 2.4 * 100) for opportunity in opportunities)
    assert all(opportunity.estimated_extra_output == 10 for opportunity in opportunities)


def test_scan_all_flattens_opportunities_into_report(monkeypatch):
    detector = CrossChainArbDetector()
    opportunity = RouteSpreadOpportunity(
        pair="SOL/USDC",
        better_quote=RouteQuote(
            amount=100,
            out_amount=210,
            output_per_input=2.1,
            route_labels=("Orca",),
            price_impact_pct=0.01,
        ),
        worse_quote=RouteQuote(
            amount=300,
            out_amount=600,
            output_per_input=2.0,
            route_labels=("Phoenix",),
            price_impact_pct=0.03,
        ),
        spread_pct=5.0,
        estimated_extra_output=10,
    )
    monkeypatch.setattr(
        detector,
        "scan_pair",
        lambda input_mint, output_mint, pair_name, amounts=None: [opportunity],
    )

    report = detector.scan_all([("mint-in", "mint-out", "SOL/USDC")], amounts=[100, 300])

    assert report["pairs_scanned"] == 1
    assert len(report["opportunities"]) == 1
    assert report["opportunities"][0] == {
        "pair": "SOL/USDC",
        "spread_pct": 5.0,
        "estimated_extra_output": 10,
        "better_quote": {
            "amount": 100,
            "out_amount": 210,
            "output_per_input": 2.1,
            "route": "Orca",
            "price_impact_pct": 0.01,
            "context_slot": None,
        },
        "worse_quote": {
            "amount": 300,
            "out_amount": 600,
            "output_per_input": 2.0,
            "route": "Phoenix",
            "price_impact_pct": 0.03,
            "context_slot": None,
        },
    }
    assert isinstance(report["timestamp"], str)
