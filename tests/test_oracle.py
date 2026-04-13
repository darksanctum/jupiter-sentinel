import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.oracle as oracle
from src.oracle import PriceFeed, PricePoint

VALID_ALT_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
VALID_OTHER_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


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

    monkeypatch.setattr(oracle.urllib.request, "urlopen", fake_urlopen)
    return calls


def make_feed(*prices):
    feed = PriceFeed("TEST/PAIR", VALID_ALT_MINT, VALID_OTHER_MINT)
    for index, price in enumerate(prices):
        feed.history.append(PricePoint(timestamp=float(index), price=price))
    return feed


def test_fetch_price_parses_sol_to_usdc_quote_and_records_history(monkeypatch):
    calls = install_urlopen(monkeypatch, {"outAmount": "150000"})
    monkeypatch.setattr(oracle.time, "time", lambda: 1234.5)

    feed = PriceFeed("SOL/USDC", oracle.SOL_MINT, oracle.USDC_MINT)
    point = feed.fetch_price()

    assert point == PricePoint(timestamp=1234.5, price=150.0)
    assert list(feed.history) == [point]
    assert feed.current_price == 150.0

    request, timeout = calls[0]
    assert timeout == 10
    assert request.full_url == (
        f"{oracle.JUPITER_SWAP_V1}/quote?"
        f"inputMint={oracle.SOL_MINT}&"
        f"outputMint={oracle.USDC_MINT}&"
        f"amount=1000000&"
        f"slippageBps=50"
    )
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"] == oracle.HEADERS["User-Agent"]
    assert headers["content-type"] == oracle.HEADERS["Content-Type"]
    if "x-api-key" in oracle.HEADERS:
        assert headers["x-api-key"] == oracle.HEADERS["x-api-key"]


def test_fetch_price_parses_non_sol_quote_to_usdc(monkeypatch):
    install_urlopen(monkeypatch, {"outAmount": "2500000"})

    feed = PriceFeed("JUP/USDC", VALID_ALT_MINT, oracle.USDC_MINT)
    point = feed.fetch_price()

    assert point is not None
    assert point.price == pytest.approx(2.5)


def test_fetch_price_converts_sol_output_using_sol_usd_lookup(monkeypatch):
    install_urlopen(monkeypatch, {"outAmount": "20000000"})
    monkeypatch.setattr(PriceFeed, "_get_sol_price", lambda self: 150.0)

    feed = PriceFeed("JUP/SOL", VALID_ALT_MINT, oracle.SOL_MINT)
    point = feed.fetch_price()

    assert point is not None
    assert point.price == pytest.approx(3.0)


def test_fetch_price_returns_zero_price_when_sol_usd_lookup_fails(monkeypatch):
    install_urlopen(monkeypatch, {"outAmount": "20000000"})
    monkeypatch.setattr(PriceFeed, "_get_sol_price", lambda self: None)

    feed = PriceFeed("JUP/SOL", VALID_ALT_MINT, oracle.SOL_MINT)
    point = feed.fetch_price()

    assert point is not None
    assert point.price == 0
    assert feed.current_price == 0


def test_fetch_price_uses_default_decimal_normalization_for_other_outputs(monkeypatch):
    install_urlopen(monkeypatch, {"outAmount": "1234567"})

    feed = PriceFeed("TOKEN/OTHER", VALID_ALT_MINT, VALID_OTHER_MINT)
    point = feed.fetch_price()

    assert point is not None
    assert point.price == pytest.approx(1.234567)


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
def test_fetch_price_returns_none_and_preserves_history_on_errors(monkeypatch, response):
    calls = install_urlopen(monkeypatch, response)
    monkeypatch.setattr(oracle, "fetch_dexscreener_price", lambda *args, **kwargs: None)
    feed = PriceFeed("SOL/USDC", oracle.SOL_MINT, oracle.USDC_MINT)

    result = feed.fetch_price()

    assert result is None
    assert list(feed.history) == []
    assert len(calls) == 1


def test_fetch_price_falls_back_to_dexscreener_when_jupiter_is_unavailable(monkeypatch):
    install_urlopen(monkeypatch, URLError("jupiter down"))
    monkeypatch.setattr(
        oracle,
        "fetch_dexscreener_price",
        lambda *args, **kwargs: {
            "price": 1.75,
            "liquidity_usd": 25_000.0,
            "source": "dexscreener",
        },
    )
    monkeypatch.setattr(oracle.time, "time", lambda: 3456.0)

    feed = PriceFeed("JUP/USDC", VALID_ALT_MINT, oracle.USDC_MINT)
    point = feed.fetch_price()

    assert point == PricePoint(timestamp=3456.0, price=1.75, volume_estimate=25_000.0, source="dexscreener")
    assert list(feed.history) == [point]


def test_fetch_price_discards_stale_history_before_appending_fresh_quote(monkeypatch):
    install_urlopen(monkeypatch, {"outAmount": "175000"})
    monkeypatch.setattr(oracle.time, "time", lambda: 10_000.0)

    feed = PriceFeed("SOL/USDC", oracle.SOL_MINT, oracle.USDC_MINT)
    feed.history.append(
        PricePoint(
            timestamp=10_000.0 - (oracle.PRICE_STALE_AFTER_SECONDS + 1.0),
            price=150.0,
        )
    )

    point = feed.fetch_price()

    assert point == PricePoint(timestamp=10_000.0, price=175.0)
    assert list(feed.history) == [point]


def test_get_sol_price_parses_quote_response(monkeypatch):
    calls = install_urlopen(monkeypatch, {"outAmount": "150000"})
    feed = PriceFeed("JUP/SOL", VALID_ALT_MINT, oracle.SOL_MINT)

    price = feed._get_sol_price()

    assert price == pytest.approx(150.0)
    request, timeout = calls[0]
    assert timeout == 10
    assert request.full_url == (
        f"{oracle.JUPITER_SWAP_V1}/quote?"
        f"inputMint={oracle.SOL_MINT}&"
        f"outputMint={oracle.USDC_MINT}&"
        f"amount=1000000&"
        f"slippageBps=50"
    )


@pytest.mark.parametrize(
    "response",
    [URLError("timeout"), b"{}", {"outAmount": "bad"}],
    ids=["network-error", "missing-out-amount", "bad-out-amount"],
)
def test_get_sol_price_returns_none_on_errors(monkeypatch, response):
    install_urlopen(monkeypatch, response)
    feed = PriceFeed("JUP/SOL", VALID_ALT_MINT, oracle.SOL_MINT)

    assert feed._get_sol_price() is None


def test_current_price_and_stats_defaults_without_history():
    feed = PriceFeed("SOL/USDC", oracle.SOL_MINT, oracle.USDC_MINT)

    assert feed.current_price is None
    assert feed.volatility == 0.0
    assert feed.price_change_pct == 0.0
    assert feed.stats() == {
        "pair": "SOL/USDC",
        "price": None,
        "volatility": 0.0,
        "change_pct": 0.0,
        "data_points": 0,
    }


def test_volatility_uses_population_stddev_of_returns():
    feed = make_feed(100.0, 110.0, 99.0)

    assert feed.volatility == pytest.approx(0.1)


def test_price_change_pct_handles_zero_starting_price():
    feed = make_feed(0.0, 10.0)

    assert feed.price_change_pct == 0.0


def test_stats_reports_latest_price_change_and_data_point_count():
    feed = make_feed(100.0, 110.0, 99.0)
    feed.pair_name = "SOL/USDC"

    stats = feed.stats()

    assert stats["pair"] == "SOL/USDC"
    assert stats["price"] == 99.0
    assert stats["volatility"] == pytest.approx(0.1)
    assert stats["change_pct"] == pytest.approx(-0.01)
    assert stats["data_points"] == 3


def test_history_keeps_only_the_most_recent_60_points():
    feed = PriceFeed("SOL/USDC", oracle.SOL_MINT, oracle.USDC_MINT)

    for index in range(61):
        feed.history.append(PricePoint(timestamp=float(index), price=float(index)))

    assert len(feed.history) == 60
    assert feed.history[0].timestamp == 1.0
    assert feed.history[-1].timestamp == 60.0
