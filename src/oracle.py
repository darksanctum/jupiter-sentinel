"""
Jupiter Sentinel - Price Oracle
Uses Jupiter swap quotes as a real-time price feed.
This is creative API usage: we treat the swap engine as an oracle.
"""
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .config import JUPITER_SWAP_V1, HEADERS, USDC_MINT, SOL_MINT
from .resilience import (
    PRICE_STALE_AFTER_SECONDS,
    fetch_dexscreener_price,
    prune_stale_price_history,
    request_json,
)
from .validation import build_jupiter_quote_url


@dataclass
class PricePoint:
    timestamp: float
    price: float
    volume_estimate: float = 0.0
    source: str = "jupiter"


@dataclass
class PriceFeed:
    """Rolling price feed using Jupiter quotes as oracle."""
    pair_name: str
    input_mint: str
    output_mint: str
    history: deque = field(default_factory=lambda: deque(maxlen=60))
    max_price_age_seconds: float = PRICE_STALE_AFTER_SECONDS

    def fetch_price(self) -> Optional[PricePoint]:
        """Get current price by quoting a small swap."""
        now = time.time()
        prune_stale_price_history(
            self.history,
            max_age_seconds=self.max_price_age_seconds,
            now=now,
            pair_name=self.pair_name,
        )

        try:
            # Use 0.001 SOL worth as quote amount (or equivalent)
            if self.input_mint == SOL_MINT:
                amount = 1_000_000  # 0.001 SOL
            else:
                amount = 1_000_000  # 1 unit
            
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                self.input_mint,
                self.output_mint,
                amount,
                50,
            )
            
            req = urllib.request.Request(url, headers=HEADERS)
            resp = request_json(req, timeout=10, describe=f"Price feed quote {self.pair_name}")
            
            out_amount = int(resp["outAmount"])
            
            # Normalize to USD price
            if self.output_mint == USDC_MINT:
                # Output is already in USDC (6 decimals)
                price = out_amount / 1e6 / (amount / 1e9) if self.input_mint == SOL_MINT else out_amount / 1e6
            elif self.output_mint == SOL_MINT:
                # Need to get SOL price separately
                sol_price = self._get_sol_price()
                if sol_price:
                    price = (out_amount / 1e9) * sol_price
                else:
                    price = 0
            else:
                price = out_amount / 1e6
            
            point = PricePoint(
                timestamp=now,
                price=price,
            )
            self.history.append(point)
            return point
        except Exception:
            fallback = fetch_dexscreener_price(self.input_mint, self.output_mint)
            if fallback is None:
                return None

            point = PricePoint(
                timestamp=now,
                price=float(fallback["price"]),
                volume_estimate=float(fallback.get("liquidity_usd", 0.0) or 0.0),
                source="dexscreener",
            )
            self.history.append(point)
            return point

    def _get_sol_price(self) -> Optional[float]:
        """Get SOL/USD price."""
        try:
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                SOL_MINT,
                USDC_MINT,
                1_000_000,
                50,
            )
            req = urllib.request.Request(url, headers=HEADERS)
            resp = request_json(req, timeout=10, describe="SOL/USD oracle quote")
            return int(resp["outAmount"]) / 1e6 / 0.001
        except Exception:
            return None
    
    @property
    def current_price(self) -> Optional[float]:
        if self.history:
            return self.history[-1].price
        return None
    
    @property
    def volatility(self) -> float:
        """Calculate rolling volatility (std dev of returns)."""
        if len(self.history) < 3:
            return 0.0
        
        prices = [p.price for p in self.history]
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        
        if not returns:
            return 0.0
        
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance ** 0.5
    
    @property
    def price_change_pct(self) -> float:
        """Price change over the tracked period."""
        if len(self.history) < 2:
            return 0.0
        first = self.history[0].price
        last = self.history[-1].price
        if first == 0:
            return 0.0
        return (last - first) / first
    
    def stats(self) -> dict:
        return {
            "pair": self.pair_name,
            "price": self.current_price,
            "volatility": self.volatility,
            "change_pct": self.price_change_pct,
            "data_points": len(self.history),
        }
