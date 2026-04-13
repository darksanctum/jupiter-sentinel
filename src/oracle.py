"""
Jupiter Sentinel - Price Oracle
Uses Jupiter swap quotes as a real-time price feed.
This is creative API usage: we treat the swap engine as an oracle.
"""
import time
import json
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .config import JUPITER_SWAP_V1, HEADERS, USDC_MINT, SOL_MINT


@dataclass
class PricePoint:
    timestamp: float
    price: float
    volume_estimate: float = 0.0


@dataclass  
class PriceFeed:
    """Rolling price feed using Jupiter quotes as oracle."""
    pair_name: str
    input_mint: str
    output_mint: str
    history: deque = field(default_factory=lambda: deque(maxlen=60))
    
    def fetch_price(self) -> Optional[PricePoint]:
        """Get current price by quoting a small swap."""
        try:
            # Use 0.001 SOL worth as quote amount (or equivalent)
            if self.input_mint == SOL_MINT:
                amount = 1_000_000  # 0.001 SOL
            else:
                amount = 1_000_000  # 1 unit
            
            url = (
                f"{JUPITER_SWAP_V1}/quote?"
                f"inputMint={self.input_mint}&"
                f"outputMint={self.output_mint}&"
                f"amount={amount}&"
                f"slippageBps=50"
            )
            
            req = urllib.request.Request(url, headers=HEADERS)
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            
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
                timestamp=time.time(),
                price=price,
            )
            self.history.append(point)
            return point
            
        except Exception as e:
            return None
    
    def _get_sol_price(self) -> Optional[float]:
        """Get SOL/USD price."""
        try:
            url = (
                f"{JUPITER_SWAP_V1}/quote?"
                f"inputMint={SOL_MINT}&"
                f"outputMint={USDC_MINT}&"
                f"amount=1000000&"  # 0.001 SOL
                f"slippageBps=50"
            )
            req = urllib.request.Request(url, headers=HEADERS)
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            return int(resp["outAmount"]) / 1e6 / 0.001
        except:
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
