"""
Jupiter Sentinel - Token Discovery
Fetches boosted tokens from DexScreener and resolves them into
tradeable Solana pairs that can feed the scanner.
"""

from __future__ import annotations
import logging

import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

from .config import SOL_MINT, USDC_MINT
from .resilience import request_json

DEXSCREENER_BASE = "https://api.dexscreener.com"
TOKEN_BOOSTS_TOP_URL = f"{DEXSCREENER_BASE}/token-boosts/top/v1"
TOKENS_BY_ADDRESS_URL = f"{DEXSCREENER_BASE}/tokens/v1"
DEXSCREENER_HEADERS = {
    "User-Agent": "JupiterSentinel/1.0",
    "Accept": "application/json",
}

SOLANA_CHAIN_ID = "solana"
SCANNER_COMPATIBLE_QUOTES = {USDC_MINT, SOL_MINT}
SCANNER_QUOTE_PRIORITY = {
    USDC_MINT: 2,
    SOL_MINT: 1,
}


def _as_float(value: Any, default: float = 0.0) -> float:
    """Function docstring."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    """Function docstring."""
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _created_at_seconds(raw_value: Any) -> Optional[float]:
    """Function docstring."""
    timestamp = _as_float(raw_value, default=0.0)
    if timestamp <= 0:
        return None
    if timestamp > 10_000_000_000:
        return timestamp / 1000.0
    return timestamp


@dataclass(frozen=True)
class TradeableToken:
    chain_id: str
    token_address: str
    symbol: str
    name: str
    pair_name: str
    pair_address: str
    dex_id: str
    pair_url: str
    input_mint: str
    output_mint: str
    quote_symbol: str
    quote_token_address: str
    price_usd: Optional[float]
    liquidity_usd: float
    liquidity_base: float
    liquidity_quote: float
    volume_24h: float
    volume_6h: float
    volume_1h: float
    volume_5m: float
    age_hours: float
    fdv: Optional[float]
    market_cap: Optional[float]
    boost_amount: float
    boost_total_amount: float
    boosts_active: int
    scanner_compatible: bool

    def to_dict(self) -> dict[str, Any]:
        """Function docstring."""
        return asdict(self)


class TokenDiscovery:
    """
    Resolves DexScreener boosted tokens into tradeable pairs.

    The current scanner normalizes output prices cleanly for USDC and SOL
    pairs, so those pools are preferred when multiple tradeable pairs exist.
    """

    def __init__(
        self,
        cache_ttl: int = 60,
        min_liquidity_usd: float = 0.0,
        min_volume_usd: float = 0.0,
        min_pair_age_hours: float = 1.0,
    ) -> None:
        """Function docstring."""
        self.cache_ttl = cache_ttl
        self.min_liquidity_usd = min_liquidity_usd
        self.min_volume_usd = min_volume_usd
        self.min_pair_age_hours = min_pair_age_hours
        self._cache: Optional[list[TradeableToken]] = None
        self._last_fetch = 0.0

    def get_tradeable_tokens(
        self,
        limit: Optional[int] = None,
        scanner_compatible_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return boosted tokens with tradeability metrics."""
        tokens = self._discover_tradeable_tokens()
        if scanner_compatible_only:
            tokens = [token for token in tokens if token.scanner_compatible]
        if limit is not None:
            tokens = tokens[:limit]
        return [token.to_dict() for token in tokens]

    def build_scan_pairs(
        self, limit: Optional[int] = None
    ) -> list[tuple[str, str, str]]:
        """Build scanner-ready `(input_mint, output_mint, pair_name)` tuples."""
        pairs: list[tuple[str, str, str]] = []
        for token in self._discover_tradeable_tokens():
            if not token.scanner_compatible:
                continue
            pairs.append((token.input_mint, token.output_mint, token.pair_name))
            if limit is not None and len(pairs) >= limit:
                break
        return pairs

    def _discover_tradeable_tokens(self) -> list[TradeableToken]:
        """Function docstring."""
        now = time.time()
        if self._cache is not None and (now - self._last_fetch) < self.cache_ttl:
            return list(self._cache)

        boosted_tokens = self._fetch_boosted_tokens()
        if not boosted_tokens:
            if self._cache is not None:
                return list(self._cache)
            self._cache = []
            self._last_fetch = now
            return []

        solana_boosts: list[dict[str, Any]] = []
        seen_addresses: set[str] = set()
        for boost in boosted_tokens:
            chain_id = str(boost.get("chainId", "")).lower()
            token_address = str(boost.get("tokenAddress", "")).strip()
            if (
                chain_id != SOLANA_CHAIN_ID
                or not token_address
                or token_address in seen_addresses
            ):
                continue
            solana_boosts.append(boost)
            seen_addresses.add(token_address)

        if not solana_boosts:
            self._cache = []
            self._last_fetch = now
            return []

        pairs_by_token = self._fetch_pairs_by_token(
            [str(boost["tokenAddress"]).strip() for boost in solana_boosts]
        )

        discovered: list[TradeableToken] = []
        for boost in solana_boosts:
            token_address = str(boost["tokenAddress"]).strip()
            pair = self._select_tradeable_pair(
                pairs_by_token.get(token_address, []), now
            )
            if not pair:
                continue
            discovered.append(self._build_tradeable_token(boost, pair, now))

        self._cache = discovered
        self._last_fetch = now
        return list(discovered)

    def _fetch_boosted_tokens(self) -> list[dict[str, Any]]:
        """Function docstring."""
        payload = self._fetch_json(TOKEN_BOOSTS_TOP_URL)
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _fetch_pairs_by_token(
        self, token_addresses: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Function docstring."""
        pairs_by_token: dict[str, list[dict[str, Any]]] = {
            address: [] for address in token_addresses
        }

        for chunk in _chunks(token_addresses, 30):
            joined = urllib.parse.quote(",".join(chunk), safe=",")
            url = f"{TOKENS_BY_ADDRESS_URL}/{SOLANA_CHAIN_ID}/{joined}"
            payload = self._fetch_json(url)
            if not isinstance(payload, list):
                continue

            for pair in payload:
                if not isinstance(pair, dict):
                    continue
                base_token = pair.get("baseToken", {}) or {}
                base_address = str(base_token.get("address", "")).strip()
                if base_address in pairs_by_token:
                    pairs_by_token[base_address].append(pair)

        return pairs_by_token

    def _fetch_json(self, url: str, timeout: int = 10) -> Any:
        """Function docstring."""
        try:
            request = urllib.request.Request(url, headers=DEXSCREENER_HEADERS)
            return request_json(
                request, timeout=timeout, describe="DexScreener request"
            )
        except Exception:
            return None

    def _select_tradeable_pair(
        self,
        pairs: list[dict[str, Any]],
        now: float,
    ) -> Optional[dict[str, Any]]:
        """Function docstring."""
        candidates = [pair for pair in pairs if self._is_tradeable_pair(pair, now)]
        if not candidates:
            return None
        return max(candidates, key=self._pair_rank)

    def _is_tradeable_pair(self, pair: dict[str, Any], now: float) -> bool:
        """Function docstring."""
        if str(pair.get("chainId", "")).lower() != SOLANA_CHAIN_ID:
            return False

        liquidity_usd = _as_float((pair.get("liquidity", {}) or {}).get("usd"))
        volume_24h = _as_float((pair.get("volume", {}) or {}).get("h24"))
        age_hours = self._pair_age_hours(pair, now)

        if liquidity_usd <= self.min_liquidity_usd:
            return False
        if volume_24h <= self.min_volume_usd:
            return False
        if age_hours <= self.min_pair_age_hours:
            return False
        return True

    def _pair_age_hours(self, pair: dict[str, Any], now: float) -> float:
        """Function docstring."""
        created_at_seconds = _created_at_seconds(pair.get("pairCreatedAt"))
        if created_at_seconds is None or created_at_seconds > now:
            return 0.0
        return (now - created_at_seconds) / 3600.0

    def _pair_rank(self, pair: dict[str, Any]) -> tuple[int, int, float, float]:
        """Function docstring."""
        quote_address = str(
            (pair.get("quoteToken", {}) or {}).get("address", "")
        ).strip()
        liquidity_usd = _as_float((pair.get("liquidity", {}) or {}).get("usd"))
        volume_24h = _as_float((pair.get("volume", {}) or {}).get("h24"))
        return (
            1 if quote_address in SCANNER_COMPATIBLE_QUOTES else 0,
            SCANNER_QUOTE_PRIORITY.get(quote_address, 0),
            liquidity_usd,
            volume_24h,
        )

    def _build_tradeable_token(
        self,
        boost: dict[str, Any],
        pair: dict[str, Any],
        now: float,
    ) -> TradeableToken:
        """Function docstring."""
        base_token = pair.get("baseToken", {}) or {}
        quote_token = pair.get("quoteToken", {}) or {}
        liquidity = pair.get("liquidity", {}) or {}
        volume = pair.get("volume", {}) or {}
        quote_address = str(quote_token.get("address", "")).strip()

        price_usd = pair.get("priceUsd")
        parsed_price_usd = (
            None if price_usd in (None, "") else _as_float(price_usd, default=0.0)
        )
        fdv = pair.get("fdv")
        parsed_fdv = None if fdv in (None, "") else _as_float(fdv, default=0.0)
        market_cap = pair.get("marketCap")
        parsed_market_cap = (
            None if market_cap in (None, "") else _as_float(market_cap, default=0.0)
        )

        return TradeableToken(
            chain_id=SOLANA_CHAIN_ID,
            token_address=str(base_token.get("address", "")).strip(),
            symbol=str(base_token.get("symbol", "")).strip(),
            name=str(base_token.get("name", "")).strip(),
            pair_name=(
                f"{str(base_token.get('symbol', '')).strip()}/"
                f"{str(quote_token.get('symbol', '')).strip()}"
            ),
            pair_address=str(pair.get("pairAddress", "")).strip(),
            dex_id=str(pair.get("dexId", "")).strip(),
            pair_url=str(pair.get("url", "")).strip(),
            input_mint=str(base_token.get("address", "")).strip(),
            output_mint=quote_address,
            quote_symbol=str(quote_token.get("symbol", "")).strip(),
            quote_token_address=quote_address,
            price_usd=parsed_price_usd,
            liquidity_usd=_as_float(liquidity.get("usd")),
            liquidity_base=_as_float(liquidity.get("base")),
            liquidity_quote=_as_float(liquidity.get("quote")),
            volume_24h=_as_float(volume.get("h24")),
            volume_6h=_as_float(volume.get("h6")),
            volume_1h=_as_float(volume.get("h1")),
            volume_5m=_as_float(volume.get("m5")),
            age_hours=self._pair_age_hours(pair, now),
            fdv=parsed_fdv,
            market_cap=parsed_market_cap,
            boost_amount=_as_float(boost.get("amount")),
            boost_total_amount=_as_float(boost.get("totalAmount")),
            boosts_active=int(_as_float((pair.get("boosts", {}) or {}).get("active"))),
            scanner_compatible=quote_address in SCANNER_COMPATIBLE_QUOTES,
        )


__all__ = ["TradeableToken", "TokenDiscovery"]
