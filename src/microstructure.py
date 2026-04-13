"""Module explaining what this file does."""

from typing import Any
import asyncio
import logging
from typing import Dict, List, Optional
import aiohttp
import time

from .config import SOL_MINT, USDC_MINT
from .resilience import async_request_json
from .security import sanitize_sensitive_text

logger = logging.getLogger(__name__)


class MicrostructureAnalyzer:
    """
    Analyzes order flow and microstructure on Jupiter by querying quotes at different sizes.
    Detects liquidity depth, price impact curves, and spread widening to determine optimal
    trade size and timing.
    """

    def __init__(self, rpc_url: str = "https://quote-api.jup.ag/v6") -> None:
        """Function docstring."""
        self.rpc_url = rpc_url
        # Sizes to query in USD equivalent (assuming USDC as input for simplicity)
        self.quote_sizes = [10, 100, 1000, 10000, 50000]
        # USDC has 6 decimals
        self.usdc_decimals = 1_000_000

    async def get_quote(
        self, input_mint: str, output_mint: str, amount: int
    ) -> Optional[Dict]:
        """Fetch a quote from Jupiter API."""
        url = f"{self.rpc_url}/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": "50",
        }

        async with aiohttp.ClientSession() as session:
            try:
                return await async_request_json(
                    session,
                    url,
                    params=params,
                    timeout=10,
                    logger=logger.warning,
                    describe="Microstructure quote",
                )
            except Exception as e:
                logger.error("Error fetching quote: %s", sanitize_sensitive_text(e))
                return None

    async def analyze_liquidity(self, input_mint: str, output_mint: str) -> Dict:
        """
        Analyze liquidity by querying multiple sizes and calculating price impact.
        Returns a dictionary containing the analysis results.
        """
        results = {
            "timestamp": time.time(),
            "token_pair": f"{input_mint}-{output_mint}",
            "curves": [],
            "optimal_size_usd": 0,
            "execution_recommendation": "WAIT",  # WAIT or EXECUTE
        }

        baseline_price = None
        acceptable_impact_threshold = 1.0  # 1% impact is our threshold for EXECUTE

        for size_usd in self.quote_sizes:
            amount_in_decimals = int(size_usd * self.usdc_decimals)
            quote = await self.get_quote(input_mint, output_mint, amount_in_decimals)

            if not quote:
                continue

            out_amount = int(quote.get("outAmount", 0))
            if out_amount == 0:
                continue

            # Calculate effective price (Output / Input)
            # Normalizing decimals would be needed for absolute price, but for relative impact we can use raw ratios
            # Assuming we want to find price impact compared to the smallest size ($10)

            effective_rate = out_amount / amount_in_decimals

            if baseline_price is None:
                baseline_price = effective_rate
                impact_pct = 0.0
            else:
                # How much worse is the rate compared to the baseline?
                impact_pct = (baseline_price - effective_rate) / baseline_price * 100

            results["curves"].append(
                {
                    "size_usd": size_usd,
                    "effective_rate": effective_rate,
                    "price_impact_pct": impact_pct,
                    "route_plan": len(quote.get("routePlan", [])),
                }
            )

            # Determine optimal size (largest size with acceptable impact)
            if impact_pct < acceptable_impact_threshold:
                results["optimal_size_usd"] = size_usd

        # Determine execution recommendation based on the $1000 size (typical retail trade)
        # If $1000 has > 2% impact, we should WAIT (high impact = wait)
        # Otherwise EXECUTE (low impact = execute now)

        high_impact_detected = False
        for curve in results["curves"]:
            if curve["size_usd"] == 1000 and curve["price_impact_pct"] > 2.0:
                high_impact_detected = True
                break

        if not high_impact_detected and len(results["curves"]) > 0:
            results["execution_recommendation"] = "EXECUTE"

        return results

    def print_analysis(self, results: Dict) -> Any:
        """Format and print the analysis results."""
        logging.debug("%s", f"--- Microstructure Analysis: {results['token_pair']} ---")
        logging.debug("%s", f"Recommendation: {results['execution_recommendation']}")
        logging.debug(
            "%s", f"Optimal Size (<= 1% impact): ${results['optimal_size_usd']}"
        )
        logging.debug("%s", "\nPrice Impact Curve:")
        logging.debug("%s", f"{'Size (USD)':<15} | {'Impact (%)':<15} | {'Hops':<10}")
        logging.debug("%s", "-" * 45)

        for curve in results["curves"]:
            logging.debug(
                "%s",
                f"${curve['size_usd']:<14} | {curve['price_impact_pct']:<14.4f} | {curve['route_plan']:<10}",
            )
        logging.debug("%s", "-" * 45)


async def main() -> Any:
    """Function docstring."""
    analyzer = MicrostructureAnalyzer()
    logging.debug("%s", "Analyzing USDC -> SOL liquidity depth...")
    results = await analyzer.analyze_liquidity(USDC_MINT, SOL_MINT)
    analyzer.print_analysis(results)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
