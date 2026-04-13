import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.token_discovery as token_discovery
from src.token_discovery import (
    DEXSCREENER_TOKEN_BOOSTS_TOP_V1,
    DEXSCREENER_TOKENS_V1,
    TokenDiscovery,
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

    monkeypatch.setattr(token_discovery.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_discover_tradeable_tokens_filters_and_selects_best_pair(monkeypatch):
    now_ts = 1_700_000_000.0
    monkeypatch.setattr(token_discovery.time, "time", lambda: now_ts)
    calls = install_urlopen(
        monkeypatch,
        [
            {
                "chainId": "solana",
                "tokenAddress": "TokenAlpha",
                "amount": 25,
                "totalAmount": 500,
                "url": "https://dexscreener.com/solana/tokenalpha",
            },
            {
                "chainId": "ethereum",
                "tokenAddress": "EthToken",
                "amount": 100,
                "totalAmount": 999,
            },
            {
                "chainId": "solana",
                "tokenAddress": "TokenBeta",
                "amount": 20,
                "totalAmount": 300,
            },
        ],
        [
            {
                "chainId": "solana",
                "dexId": "raydium",
                "url": "https://dexscreener.com/solana/pair-fresh",
                "pairAddress": "pair-fresh",
                "baseToken": {
                    "address": "TokenAlpha",
                    "name": "Alpha",
                    "symbol": "ALPHA",
                },
                "quoteToken": {
                    "address": "USDC",
                    "name": "USD Coin",
                    "symbol": "USDC",
                },
                "priceNative": "0.0002",
                "priceUsd": "0.12",
                "txns": {"h24": {"buys": 10, "sells": 8}},
                "volume": {"h24": 9_000},
                "liquidity": {"usd": 4_000},
                "fdv": 120_000,
                "marketCap": 90_000,
                "pairCreatedAt": int((now_ts - 1_800) * 1000),
                "boosts": {"active": 2},
            },
            {
                "chainId": "solana",
                "dexId": "orca",
                "url": "https://dexscreener.com/solana/pair-old",
                "pairAddress": "pair-old",
                "baseToken": {
                    "address": "TokenAlpha",
                    "name": "Alpha",
                    "symbol": "ALPHA",
                },
                "quoteToken": {
                    "address": "USDC",
                    "name": "USD Coin",
                    "symbol": "USDC",
                },
                "priceNative": "0.0003",
                "priceUsd": "0.15",
                "txns": {"h24": {"buys": 40, "sells": 31}},
                "volume": {"h24": 15_000},
                "liquidity": {"usd": 11_000},
                "fdv": 150_000,
                "marketCap": 110_000,
                "pairCreatedAt": int((now_ts - 7_200) * 1000),
                "boosts": {"active": 4},
            },
            {
                "chainId": "solana",
                "dexId": "raydium",
                "url": "https://dexscreener.com/solana/token-beta",
                "pairAddress": "pair-beta",
                "baseToken": {
                    "address": "TokenBeta",
                    "name": "Beta",
                    "symbol": "BETA",
                },
                "quoteToken": {
                    "address": "USDC",
                    "name": "USD Coin",
                    "symbol": "USDC",
                },
                "priceNative": "0.0001",
                "priceUsd": "0.05",
                "txns": {"h24": {"buys": 4, "sells": 2}},
                "volume": {"h24": 0},
                "liquidity": {"usd": 5_000},
                "fdv": 50_000,
                "marketCap": 40_000,
                "pairCreatedAt": int((now_ts - 9_000) * 1000),
                "boosts": {"active": 1},
            },
        ],
    )

    discovery = TokenDiscovery()

    tokens = discovery.discover_tradeable_tokens()

    assert tokens == [
        {
            "chain_id": "solana",
            "token_address": "TokenAlpha",
            "name": "Alpha",
            "symbol": "ALPHA",
            "pair": "ALPHA/USDC",
            "pair_address": "pair-old",
            "dex_id": "orca",
            "quote_token_address": "USDC",
            "quote_token_symbol": "USDC",
            "price_usd": 0.15,
            "price_native": 0.0003,
            "liquidity_usd": 11_000.0,
            "volume_24h": 15_000.0,
            "buys_24h": 40,
            "sells_24h": 31,
            "age_hours": 2.0,
            "fdv": 150_000.0,
            "market_cap": 110_000.0,
            "boost_amount": 25.0,
            "boost_total_amount": 500.0,
            "active_boosts": 4,
            "pair_url": "https://dexscreener.com/solana/pair-old",
        }
    ]

    first_request, first_timeout = calls[0]
    assert first_timeout == 10
    assert first_request.full_url == DEXSCREENER_TOKEN_BOOSTS_TOP_V1

    second_request, second_timeout = calls[1]
    assert second_timeout == 10
    assert second_request.full_url == f"{DEXSCREENER_TOKENS_V1}/solana/TokenAlpha,TokenBeta"


def test_fetch_pairs_for_tokens_batches_requests_in_groups_of_thirty(monkeypatch):
    token_addresses = [f"Token{i:02d}" for i in range(31)]
    calls = install_urlopen(
        monkeypatch,
        [
            {
                "chainId": "solana",
                "pairAddress": "pair-00",
                "baseToken": {"address": "Token00", "symbol": "TK0"},
                "quoteToken": {"address": "USDC", "symbol": "USDC"},
            }
        ],
        [
            {
                "chainId": "solana",
                "pairAddress": "pair-30",
                "baseToken": {"address": "Token30", "symbol": "TK30"},
                "quoteToken": {"address": "USDC", "symbol": "USDC"},
            }
        ],
    )

    discovery = TokenDiscovery()

    pairs_by_token = discovery.fetch_pairs_for_tokens(token_addresses)

    assert len(calls) == 2
    assert calls[0][0].full_url == (
        f"{DEXSCREENER_TOKENS_V1}/solana/"
        + ",".join(token_addresses[:30])
    )
    assert calls[1][0].full_url == f"{DEXSCREENER_TOKENS_V1}/solana/{token_addresses[30]}"
    assert [pair["pairAddress"] for pair in pairs_by_token["Token00"]] == ["pair-00"]
    assert [pair["pairAddress"] for pair in pairs_by_token["Token30"]] == ["pair-30"]
