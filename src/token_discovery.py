"""
Jupiter Sentinel - Token Discovery
Fetches boosted tokens from DexScreener and filters them down to
tradeable Solana opportunities for downstream scanners.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .config import HEADERS

DEXSCREENER_BASE = "https://api.dexscreener.com"
DEXSCREENER_TOKEN_BOOSTS_TOP_V1 = f"{DEXSCREENER_BASE}/token-boosts/top/v1"
DEXSCREENER_TOKENS_V1 = f"{DEXSCREENER_BASE}/tokens/v1"
DEXSCREENER_SOLANA_CHAIN_ID = "solana"
MAX_TOKEN_BATCH_SIZE = 30
DEFAULT_MIN_AGE_SECS = 60 * 60


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _chunked(items: Iterable[str], size: int) -> Iterable[list[str]]:
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _normalize_timestamp(value: Any) -> Optional[float]:
    raw = _safe_float(value, default=-1.0)
    if raw <= 0:
        return None
    if raw > 1_000_000_000_000:
        return raw / 1000.0
    return raw


@dataclass(frozen=True)
class TokenBoost:
    chain_id: str
    token_address: str
    amount: float
    total_amount: float
    url: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Optional["TokenBoost"]:
        token_address = str(payload.get("tokenAddress", "")).strip()
        if not token_address:
            return None

        amount = _safe_float(payload.get("amount"))
        total_amount = _safe_float(payload.get("totalAmount"), default=amount)
        return cls(
            chain_id=str(payload.get("chainId", "")).strip(),
            token_address=token_address,
            amount=amount,
            total_amount=total_amount,
            url=payload.get("url"),
        )


@dataclass(frozen=True)
class TradeableToken:
    chain_id: str
    token_address: str
    name: str
    symbol: str
    pair: str
    pair_address: str
    dex_id: str
    quote_token_address: str
    quote_token_symbol: str
    price_usd: float
    price_native: float
    liquidity_usd: float
    volume_24h: float
    buys_24h: int
    sells_24h: int
    age_hours: float
    fdv: float
    market_cap: float
    boost_amount: float
    boost_total_amount: float
    active_boosts: int
    pair_url: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "token_address": self.token_address,
            "name": self.name,
            "symbol": self.symbol,
            "pair": self.pair,
            "pair_address": self.pair_address,
            "dex_id": self.dex_id,
            "quote_token_address": self.quote_token_address,
            "quote_token_symbol": self.quote_token_symbol,
            "price_usd": self.price_usd,
            "price_native": self.price_native,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h": self.volume_24h,
            "buys_24h": self.buys_24h,
            "sells_24h": self.sells_24h,
            "age_hours": self.age_hours,
            "fdv": self.fdv,
            "market_cap": self.market_cap,
            "boost_amount": self.boost_amount,
            "boost_total_amount": self.boost_total_amount,
            "active_boosts": self.active_boosts,
            "pair_url": self.pair_url,
        }


class TokenDiscovery:
    """Discover boosted tokens that already have tradeable Solana liquidity."""

    def __init__(
        self,
        *,
        chain_id: str = DEXSCREENER_SOLANA_CHAIN_ID,
        min_liquidity_usd: float = 0.0,
        min_volume_24h: float = 0.0,
        min_age_secs: float = DEFAULT_MIN_AGE_SECS,
        request_timeout: int = 10,
    ) -> None:
        self.chain_id = chain_id
        self.min_liquidity_usd = float(min_liquidity_usd)
        self.min_volume_24h = float(min_volume_24h)
        self.min_age_secs = float(min_age_secs)
        self.request_timeout = request_timeout

    def _request_json(self, url: str) -> Any:
        request = urllib.request.Request(url, headers=HEADERS)
        return json.loads(urllib.request.urlopen(request, timeout=self.request_timeout).read())

    def fetch_trending_boosts(self) -> list[TokenBoost]:
        """Return boosted tokens from DexScreener for the configured chain."""
        try:
            payload = self._request_json(DEXSCREENER_TOKEN_BOOSTS_TOP_V1)
        except Exception:
            return []

        boosts: list[TokenBoost] = []
        seen: set[str] = set()
        for item in payload if isinstance(payload, list) else []:
            boost = TokenBoost.from_payload(item)
            if not boost or boost.chain_id != self.chain_id:
                continue
            if boost.token_address in seen:
                continue
            seen.add(boost.token_address)
            boosts.append(boost)
        return boosts

    def fetch_pairs_for_tokens(self, token_addresses: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Batch-fetch pair metadata for up to 30 token addresses per request."""
        pairs_by_token = {token_address: [] for token_address in token_addresses}
        requested = set(token_addresses)

        for batch in _chunked(token_addresses, MAX_TOKEN_BATCH_SIZE):
            url = f"{DEXSCREENER_TOKENS_V1}/{self.chain_id}/{','.join(batch)}"
            try:
                payload = self._request_json(url)
            except Exception:
                continue

            for pair in payload if isinstance(payload, list) else []:
                base_address = str(pair.get("baseToken", {}).get("address", "")).strip()
                quote_address = str(pair.get("quoteToken", {}).get("address", "")).strip()

                if base_address in requested:
                    pairs_by_token[base_address].append(pair)
                if quote_address in requested and quote_address != base_address:
                    pairs_by_token[quote_address].append(pair)

        return pairs_by_token

    def _build_tradeable_token(
        self,
        boost: TokenBoost,
        pair: dict[str, Any],
        *,
        now_ts: float,
    ) -> Optional[TradeableToken]:
        if str(pair.get("chainId", "")).strip() != self.chain_id:
            return None

        base_token = pair.get("baseToken", {}) or {}
        quote_token = pair.get("quoteToken", {}) or {}
        base_address = str(base_token.get("address", "")).strip()
        quote_address = str(quote_token.get("address", "")).strip()

        if boost.token_address == base_address:
            token_meta = base_token
            other_token = quote_token
        elif boost.token_address == quote_address:
            token_meta = quote_token
            other_token = base_token
        else:
            return None

        liquidity_usd = _safe_float(pair.get("liquidity", {}).get("usd"))
        volume_24h = _safe_float(pair.get("volume", {}).get("h24"))
        created_at = _normalize_timestamp(pair.get("pairCreatedAt"))
        if created_at is None:
            return None

        age_secs = now_ts - created_at
        if (
            liquidity_usd <= self.min_liquidity_usd
            or volume_24h <= self.min_volume_24h
            or age_secs <= self.min_age_secs
        ):
            return None

        txns_24h = pair.get("txns", {}).get("h24", {}) or {}
        return TradeableToken(
            chain_id=self.chain_id,
            token_address=boost.token_address,
            name=str(token_meta.get("name", "")).strip(),
            symbol=str(token_meta.get("symbol", "")).strip(),
            pair=(
                f"{str(base_token.get('symbol', '')).strip()}/"
                f"{str(quote_token.get('symbol', '')).strip()}"
            ).strip("/"),
            pair_address=str(pair.get("pairAddress", "")).strip(),
            dex_id=str(pair.get("dexId", "")).strip(),
            quote_token_address=str(other_token.get("address", "")).strip(),
            quote_token_symbol=str(other_token.get("symbol", "")).strip(),
            price_usd=_safe_float(pair.get("priceUsd")),
            price_native=_safe_float(pair.get("priceNative")),
            liquidity_usd=liquidity_usd,
            volume_24h=volume_24h,
            buys_24h=_safe_int(txns_24h.get("buys")),
            sells_24h=_safe_int(txns_24h.get("sells")),
            age_hours=round(age_secs / 3600.0, 2),
            fdv=_safe_float(pair.get("fdv")),
            market_cap=_safe_float(pair.get("marketCap")),
            boost_amount=boost.amount,
            boost_total_amount=boost.total_amount,
            active_boosts=_safe_int(pair.get("boosts", {}).get("active")),
            pair_url=pair.get("url") or boost.url,
        )

    def discover_tradeable_tokens(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """
        Return boosted Solana tokens that have positive liquidity, positive 24h volume,
        and are older than the minimum age threshold.
        """
        boosts = self.fetch_trending_boosts()
        if not boosts:
            return []

        pairs_by_token = self.fetch_pairs_for_tokens([boost.token_address for boost in boosts])
        now_ts = time.time()
        discovered: list[TradeableToken] = []

        for boost in boosts:
            candidates = [
                tradeable
                for pair in pairs_by_token.get(boost.token_address, [])
                if (
                    tradeable := self._build_tradeable_token(
                        boost,
                        pair,
                        now_ts=now_ts,
                    )
                )
            ]
            if not candidates:
                continue

            best_pair = max(
                candidates,
                key=lambda item: (
                    item.liquidity_usd,
                    item.volume_24h,
                    item.active_boosts,
                    item.age_hours,
                ),
            )
            discovered.append(best_pair)

        discovered.sort(
            key=lambda item: (
                item.boost_total_amount,
                item.liquidity_usd,
                item.volume_24h,
            ),
            reverse=True,
        )

        if limit is not None:
            discovered = discovered[:limit]
        return [item.to_dict() for item in discovered]


def discover_tradeable_tokens(
    limit: Optional[int] = None,
    *,
    chain_id: str = DEXSCREENER_SOLANA_CHAIN_ID,
    min_liquidity_usd: float = 0.0,
    min_volume_24h: float = 0.0,
    min_age_secs: float = DEFAULT_MIN_AGE_SECS,
    request_timeout: int = 10,
) -> list[dict[str, Any]]:
    """Convenience wrapper for scanner integrations."""
    discovery = TokenDiscovery(
        chain_id=chain_id,
        min_liquidity_usd=min_liquidity_usd,
        min_volume_24h=min_volume_24h,
        min_age_secs=min_age_secs,
        request_timeout=request_timeout,
    )
    return discovery.discover_tradeable_tokens(limit=limit)


if __name__ == "__main__":
    print(json.dumps(discover_tradeable_tokens(), indent=2))
