"""
Jupiter Sentinel - Route Arbitrage Detector
Detects price discrepancies between different swap routes on Jupiter.
"""
import urllib.request
from typing import Any, List, Optional
from dataclasses import dataclass
from datetime import datetime

from .config import JUPITER_SWAP_V1, HEADERS, SOL_MINT, USDC_MINT
from .resilience import request_json
from .validation import build_jupiter_quote_url


@dataclass
class ArbitrageOpportunity:
    pair: str
    buy_price: float
    sell_price: float
    spread_pct: float
    buy_route: str
    sell_route: str
    estimated_profit_usd: float


class RouteArbitrage:
    """
    Detects arbitrage opportunities by comparing different routes
    through Jupiter's swap engine.
    
    Creative usage: We use Jupiter's own routing to find price
    discrepancies that Jupiter itself doesn't surface. By querying
    the same pair with different parameters, we can detect route
    inefficiencies.
    """
    
    def __init__(self) -> None:
        self.opportunities: List[ArbitrageOpportunity] = []
    
    def scan_pair(
        self,
        input_mint: str,
        output_mint: str,
        pair_name: str,
        amounts: Optional[List[int]] = None,
    ) -> List[ArbitrageOpportunity]:
        """
        Scan a pair for route arbitrage by quoting different amounts.
        Different amounts may route through different DEXes, creating
        price discrepancies.
        """
        if amounts is None:
            amounts = [100_000, 500_000, 1_000_000, 5_000_000]  # Different sizes
        
        quotes = []
        
        for amount in amounts:
            try:
                url = build_jupiter_quote_url(
                    JUPITER_SWAP_V1,
                    input_mint,
                    output_mint,
                    amount,
                    50,
                    only_direct_routes=False,
                )
                req = urllib.request.Request(url, headers=HEADERS)
                resp = request_json(req, timeout=10, describe=f"Route arbitrage quote {pair_name}")
                
                out_amount = int(resp["outAmount"])
                routes = resp.get("routePlan", [])
                route_labels = [r.get("swapInfo", {}).get("label", "?") for r in routes]
                
                # Price per unit
                if input_mint == SOL_MINT:
                    in_units = amount / 1e9
                else:
                    in_units = amount / 1e6
                
                if output_mint == USDC_MINT:
                    out_units = out_amount / 1e6
                elif output_mint == SOL_MINT:
                    out_units = out_amount / 1e9
                else:
                    out_units = out_amount / 1e6
                
                price_per_unit = out_units / in_units if in_units > 0 else 0
                
                quotes.append({
                    "amount": amount,
                    "out_amount": out_amount,
                    "price_per_unit": price_per_unit,
                    "routes": route_labels,
                    "price_impact": float(resp.get("priceImpactPct", 0)),
                })
                
            except Exception:
                continue
        
        # Find price discrepancies between routes
        opps = []
        for i, q1 in enumerate(quotes):
            for q2 in quotes[i+1:]:
                if q1["price_per_unit"] == 0 or q2["price_per_unit"] == 0:
                    continue
                
                spread = abs(q1["price_per_unit"] - q2["price_per_unit"]) / min(q1["price_per_unit"], q2["price_per_unit"])
                
                if spread > 0.005:  # 0.5% spread
                    buy = q1 if q1["price_per_unit"] < q2["price_per_unit"] else q2
                    sell = q2 if q1["price_per_unit"] < q2["price_per_unit"] else q1
                    
                    # Estimate profit (before gas)
                    trade_size_usd = 2.0  # $2 trade
                    est_profit = trade_size_usd * spread
                    
                    opp = ArbitrageOpportunity(
                        pair=pair_name,
                        buy_price=buy["price_per_unit"],
                        sell_price=sell["price_per_unit"],
                        spread_pct=spread * 100,
                        buy_route=" -> ".join(buy["routes"]),
                        sell_route=" -> ".join(sell["routes"]),
                        estimated_profit_usd=est_profit,
                    )
                    opps.append(opp)
                    self.opportunities.append(opp)
        
        return opps
    
    def scan_all(self, pairs: List[tuple[str, str, str]]) -> dict[str, Any]:
        """Scan all configured pairs for arbitrage."""
        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "pairs_scanned": len(pairs),
            "opportunities": [],
        }
        
        for input_mint, output_mint, name in pairs:
            opps = self.scan_pair(input_mint, output_mint, name)
            if opps:
                report["opportunities"].extend([{
                    "pair": o.pair,
                    "spread": f"{o.spread_pct:.2f}%",
                    "buy_route": o.buy_route,
                    "sell_route": o.sell_route,
                    "est_profit": f"${o.estimated_profit_usd:.4f}",
                } for o in opps])
        
        return report


if __name__ == "__main__":
    arb = RouteArbitrage()
    
    # Test with SOL/USDC
    opps = arb.scan_pair(SOL_MINT, USDC_MINT, "SOL/USDC")
    
    if opps:
        print(f"Found {len(opps)} arbitrage opportunities:")
        for o in opps:
            print(f"  {o.pair}: {o.spread_pct:.2f}% spread")
            print(f"    Buy via: {o.buy_route}")
            print(f"    Sell via: {o.sell_route}")
    else:
        print("No arbitrage opportunities found (market is efficient)")
