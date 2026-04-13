"""
Jupiter Sentinel - DEX Route Intelligence
Maps Jupiter's routing decisions across DEXes for arbitrage opportunities.
Uses the /swap/v1/program-id-to-label endpoint + route plan analysis.
"""
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import JUPITER_SWAP_V1, HEADERS, SCAN_PAIRS, SOL_MINT, USDC_MINT
from .resilience import request_json
from .validation import build_jupiter_quote_url


@dataclass
class RouteLeg:
    program_id: str
    dex_label: str
    in_amount: int
    out_amount: int
    fee_bps: Optional[float] = None


@dataclass
class RouteAnalysis:
    pair: str
    input_amount: int
    output_amount: int
    price_impact: float
    route_legs: List[RouteLeg]
    dex_path: List[str]
    route_label: str  # e.g., "Raydium -> Orca"


class DexRouteIntel:
    """Analyzes Jupiter's DEX routing decisions across trade sizes."""

    def __init__(self) -> None:
        self.dex_labels = self._load_dex_labels()
        self.route_cache: Dict[str, List[RouteAnalysis]] = {}

    def _load_dex_labels(self) -> Dict[str, str]:
        """Fetch the program-id-to-label mapping from Jupiter."""
        try:
            url = f"{JUPITER_SWAP_V1}/program-id-to-label"
            req = urllib.request.Request(url, headers=HEADERS)
            resp = request_json(req, timeout=10, describe="Jupiter program-id-to-label")
            return resp
        except Exception:
            return {}

    def _get_label(self, program_id: str) -> str:
        return self.dex_labels.get(program_id, f"Unknown({program_id[:8]}...)")

    def analyze_route(self, input_mint: str, output_mint: str,
                      amount: int, slippage_bps: int = 50) -> Optional[RouteAnalysis]:
        """Get route analysis for a specific pair and amount."""
        try:
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                input_mint,
                output_mint,
                amount,
                slippage_bps,
            )
            req = urllib.request.Request(url, headers=HEADERS)
            resp = request_json(req, timeout=10, describe="DEX route analysis")

            legs = []
            dex_path = []
            for step in resp.get("routePlan", []):
                pid = step.get("swapInfo", {}).get("ammKey", "")
                program = step.get("swapInfo", {}).get("programId", "")
                label = self._get_label(program)
                in_amt = int(step.get("swapInfo", {}).get("inAmount", 0))
                out_amt = int(step.get("swapInfo", {}).get("outAmount", 0))
                legs.append(RouteLeg(
                    program_id=program,
                    dex_label=label,
                    in_amount=in_amt,
                    out_amount=out_amt,
                ))
                dex_path.append(label)

            route_label = " -> ".join(dex_path) if dex_path else "Direct"

            return RouteAnalysis(
                pair=f"{input_mint[:8]}.../{output_mint[:8]}...",
                input_amount=amount,
                output_amount=int(resp.get("outAmount", 0)),
                price_impact=float(resp.get("priceImpactPct", 0)),
                route_legs=legs,
                dex_path=dex_path,
                route_label=route_label,
            )
        except Exception:
            return None

    def find_route_discrepancies(
        self,
        input_mint: str,
        output_mint: str,
        amounts: Optional[List[int]] = None,
    ) -> List[dict[str, Any]]:
        """Quote same pair at different sizes to find routing discrepancies."""
        if amounts is None:
            amounts = [1_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000]

        analyses = []
        for amt in amounts:
            ra = self.analyze_route(input_mint, output_mint, amt)
            if ra:
                analyses.append({
                    "amount_lamports": amt,
                    "amount_sol": amt / 1e9,
                    "out_amount": ra.output_amount,
                    "dex_path": ra.route_label,
                    "price_impact": ra.price_impact,
                    "legs": len(ra.route_legs),
                })

        # Find discrepancies: same pair, different routes
        discrepancies = []
        if len(analyses) >= 2:
            routes_seen = defaultdict(list)
            for a in analyses:
                routes_seen[a["dex_path"]].append(a)

            if len(routes_seen) > 1:
                for route, items in routes_seen.items():
                    discrepancies.append({
                        "route": route,
                        "sizes": [f"{i['amount_sol']:.4f} SOL" for i in items],
                        "avg_price_impact": sum(i["price_impact"] for i in items) / len(items),
                    })

        return discrepancies

    def get_dex_usage_stats(self) -> Dict[str, int]:
        """Which DEXes does Jupiter route through most?"""
        stats = defaultdict(int)
        for input_mint, output_mint, name in SCAN_PAIRS:
            for amt in [1_000_000, 10_000_000]:
                ra = self.analyze_route(input_mint, output_mint, amt)
                if ra:
                    for leg in ra.route_legs:
                        stats[leg.dex_label] += 1
        return dict(sorted(stats.items(), key=lambda x: -x[1]))

    def summary(self) -> str:
        """Generate a summary report."""
        lines = ["DEX Route Intelligence Report", "=" * 40]
        lines.append(f"Known DEXes: {len(self.dex_labels)}")
        for pid, label in sorted(self.dex_labels.items(), key=lambda x: x[1]):
            lines.append(f"  {label}: {pid[:20]}...")
        return "\n".join(lines)


if __name__ == "__main__":
    intel = DexRouteIntel()
    print(intel.summary())
    print()

    # Find discrepancies for SOL/USDC
    disc = intel.find_route_discrepancies(SOL_MINT, USDC_MINT)
    if disc:
        print("Route discrepancies found:")
        for d in disc:
            print(f"  {d['route']}: sizes={d['sizes']}, avg_impact={d['avg_price_impact']:.4f}%")
    else:
        print("No route discrepancies (single route used)")
