import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.token_discovery as token_discovery
from src.config import SOL_MINT, USDC_MINT
from src.token_discovery import TokenDiscovery

TOKEN_ALPHA = "Alpha111111111111111111111111111111111111111"
TOKEN_BETA = "Beta1111111111111111111111111111111111111111"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2Fy2uWbT2kHf8N13kYx4xXfY"


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

    monkeypatch.setattr(token_discovery.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_get_tradeable_tokens_filters_for_solana_liquidity_volume_and_age(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(token_discovery.time, "time", lambda: now)

    calls = install_urlopen(
        monkeypatch,
        [
            {"chainId": "solana", "tokenAddress": TOKEN_ALPHA, "amount": 120, "totalAmount": 500},
            {"chainId": "ethereum", "tokenAddress": "0xdeadbeef", "amount": 50, "totalAmount": 60},
            {"chainId": "solana", "tokenAddress": TOKEN_BETA, "amount": 90, "totalAmount": 120},
        ],
        [
            {
                "chainId": "solana",
                "pairAddress": "young-alpha",
                "dexId": "raydium",
                "url": "https://dexscreener.com/solana/young-alpha",
                "baseToken": {"address": TOKEN_ALPHA, "symbol": "ALP", "name": "Alpha"},
                "quoteToken": {"address": USDC_MINT, "symbol": "USDC", "name": "USD Coin"},
                "priceUsd": "0.12",
                "liquidity": {"usd": 2_500, "base": 20_000, "quote": 2_500},
                "volume": {"h24": 8_000, "h6": 2_000, "h1": 500, "m5": 40},
                "pairCreatedAt": int((now - 45 * 60) * 1000),
                "boosts": {"active": 1},
            },
            {
                "chainId": "solana",
                "pairAddress": "alpha-sol",
                "dexId": "orca",
                "url": "https://dexscreener.com/solana/alpha-sol",
                "baseToken": {"address": TOKEN_ALPHA, "symbol": "ALP", "name": "Alpha"},
                "quoteToken": {"address": SOL_MINT, "symbol": "SOL", "name": "Solana"},
                "priceUsd": "0.15",
                "liquidity": {"usd": 12_000, "base": 80_000, "quote": 100},
                "volume": {"h24": 25_000, "h6": 10_000, "h1": 1_200, "m5": 60},
                "pairCreatedAt": int((now - 4 * 3600) * 1000),
                "boosts": {"active": 2},
                "fdv": "2000000",
                "marketCap": "1500000",
            },
            {
                "chainId": "solana",
                "pairAddress": "alpha-usdc",
                "dexId": "raydium",
                "url": "https://dexscreener.com/solana/alpha-usdc",
                "baseToken": {"address": TOKEN_ALPHA, "symbol": "ALP", "name": "Alpha"},
                "quoteToken": {"address": USDC_MINT, "symbol": "USDC", "name": "USD Coin"},
                "priceUsd": "0.16",
                "liquidity": {"usd": 15_000, "base": 90_000, "quote": 15_000},
                "volume": {"h24": 30_000, "h6": 12_000, "h1": 2_500, "m5": 100},
                "pairCreatedAt": int((now - 2 * 3600) * 1000),
                "boosts": {"active": 3},
                "fdv": "2500000",
                "marketCap": "1800000",
            },
            {
                "chainId": "solana",
                "pairAddress": "beta-usdc",
                "dexId": "raydium",
                "url": "https://dexscreener.com/solana/beta-usdc",
                "baseToken": {"address": TOKEN_BETA, "symbol": "BET", "name": "Beta"},
                "quoteToken": {"address": USDC_MINT, "symbol": "USDC", "name": "USD Coin"},
                "priceUsd": "0.03",
                "liquidity": {"usd": 0, "base": 100_000, "quote": 0},
                "volume": {"h24": 12_000, "h6": 2_000, "h1": 100, "m5": 4},
                "pairCreatedAt": int((now - 3 * 3600) * 1000),
                "boosts": {"active": 1},
            },
        ],
    )

    discovery = TokenDiscovery()
    tokens = discovery.get_tradeable_tokens()

    assert len(tokens) == 1
    assert tokens[0] == {
        "chain_id": "solana",
        "token_address": TOKEN_ALPHA,
        "symbol": "ALP",
        "name": "Alpha",
        "pair_name": "ALP/USDC",
        "pair_address": "alpha-usdc",
        "dex_id": "raydium",
        "pair_url": "https://dexscreener.com/solana/alpha-usdc",
        "input_mint": TOKEN_ALPHA,
        "output_mint": USDC_MINT,
        "quote_symbol": "USDC",
        "quote_token_address": USDC_MINT,
        "price_usd": pytest.approx(0.16),
        "liquidity_usd": pytest.approx(15_000.0),
        "liquidity_base": pytest.approx(90_000.0),
        "liquidity_quote": pytest.approx(15_000.0),
        "volume_24h": pytest.approx(30_000.0),
        "volume_6h": pytest.approx(12_000.0),
        "volume_1h": pytest.approx(2_500.0),
        "volume_5m": pytest.approx(100.0),
        "age_hours": pytest.approx(2.0),
        "fdv": pytest.approx(2_500_000.0),
        "market_cap": pytest.approx(1_800_000.0),
        "boost_amount": pytest.approx(120.0),
        "boost_total_amount": pytest.approx(500.0),
        "boosts_active": 3,
        "scanner_compatible": True,
    }

    request, timeout = calls[0]
    assert timeout == 10
    assert request.full_url == token_discovery.TOKEN_BOOSTS_TOP_URL
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"] == token_discovery.DEXSCREENER_HEADERS["User-Agent"]
    assert headers["accept"] == token_discovery.DEXSCREENER_HEADERS["Accept"]

    request, timeout = calls[1]
    assert timeout == 10
    assert request.full_url == (
        f"{token_discovery.TOKENS_BY_ADDRESS_URL}/solana/{TOKEN_ALPHA},{TOKEN_BETA}"
    )


def test_build_scan_pairs_skips_non_scanner_compatible_quotes(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr(token_discovery.time, "time", lambda: now)

    install_urlopen(
        monkeypatch,
        [
            {"chainId": "solana", "tokenAddress": TOKEN_ALPHA, "amount": 50, "totalAmount": 100},
        ],
        [
            {
                "chainId": "solana",
                "pairAddress": "alpha-usdt",
                "dexId": "raydium",
                "url": "https://dexscreener.com/solana/alpha-usdt",
                "baseToken": {"address": TOKEN_ALPHA, "symbol": "ALP", "name": "Alpha"},
                "quoteToken": {"address": USDT_MINT, "symbol": "USDT", "name": "Tether"},
                "priceUsd": "0.20",
                "liquidity": {"usd": 22_000, "base": 110_000, "quote": 22_000},
                "volume": {"h24": 40_000, "h6": 12_000, "h1": 2_000, "m5": 75},
                "pairCreatedAt": int((now - 2 * 3600) * 1000),
                "boosts": {"active": 1},
            },
        ],
    )

    discovery = TokenDiscovery(cache_ttl=0)

    tokens = discovery.get_tradeable_tokens()
    scan_pairs = discovery.build_scan_pairs()

    assert len(tokens) == 1
    assert tokens[0]["pair_name"] == "ALP/USDT"
    assert tokens[0]["scanner_compatible"] is False
    assert scan_pairs == []


def test_get_tradeable_tokens_returns_empty_on_upstream_errors(monkeypatch):
    install_urlopen(monkeypatch, URLError("network down"))

    discovery = TokenDiscovery(cache_ttl=0)

    assert discovery.get_tradeable_tokens() == []
