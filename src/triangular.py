"""
Jupiter Sentinel - Cross-Route Arbitrage Detector
Detects price discrepancies between SOL->Token and Token->SOL routes.
"""

import logging
import urllib.request
from typing import Any, Dict, List
from datetime import datetime

from .config import JUPITER_SWAP_V1, HEADERS, SOL_MINT, USDC_MINT, SCAN_PAIRS
from .resilience import request_json
from .validation import build_jupiter_quote_url


def detect_triangular_arb() -> List[Dict[str, Any]]:
    """
    Detect triangular arbitrage: SOL -> Token -> USDC -> SOL
    If the product of the three exchange rates > 1, there's profit.

    This uses Jupiter's swap engine creatively: we quote three legs
    of a triangle and check if the loop is profitable.
    """
    logging.debug("%s", "Triangular Arbitrage Scanner")
    logging.debug("%s", "=" * 50)

    # Get SOL/USDC rate
    try:
        url = build_jupiter_quote_url(
            JUPITER_SWAP_V1, SOL_MINT, USDC_MINT, 1_000_000, 10
        )
        req = urllib.request.Request(url, headers=HEADERS)
        resp = request_json(req, timeout=10, describe="Triangular arb SOL quote")
        sol_usdc = int(resp["outAmount"]) / 1e6 / 0.001  # USDC per SOL
    except Exception:
        logging.debug("%s", "Could not get SOL/USDC rate")
        return []

    logging.debug("%s", f"SOL/USDC: ${sol_usdc:.2f}")
    logging.debug("")

    opportunities: List[Dict[str, Any]] = []

    for input_mint, output_mint, name in SCAN_PAIRS:
        if name == "SOL/USDC":
            continue

        token_mint = input_mint if input_mint != SOL_MINT else output_mint
        if token_mint == SOL_MINT or token_mint == USDC_MINT:
            continue

        try:
            # Leg 1: SOL -> Token (100k lamports)
            url1 = build_jupiter_quote_url(
                JUPITER_SWAP_V1, SOL_MINT, token_mint, 100_000, 10
            )
            req1 = urllib.request.Request(url1, headers=HEADERS)
            resp1 = request_json(
                req1, timeout=10, describe=f"Triangular arb leg 1 {name}"
            )
            sol_to_token = int(resp1["outAmount"])

            # Leg 2: Token -> USDC
            url2 = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                token_mint,
                USDC_MINT,
                sol_to_token,
                10,
            )
            req2 = urllib.request.Request(url2, headers=HEADERS)
            resp2 = request_json(
                req2, timeout=10, describe=f"Triangular arb leg 2 {name}"
            )
            token_to_usdc = int(resp2["outAmount"]) / 1e6

            # Leg 3: USDC -> SOL (implied)
            usdc_to_sol = token_to_usdc / sol_usdc  # SOL we'd get back

            # Check: did we end up with more SOL than we started?
            initial_sol = 0.0001  # 100k lamports
            profit_pct = (usdc_to_sol - initial_sol) / initial_sol * 100

            status = "PROFIT" if profit_pct > 0 else "LOSS"

            logging.debug(
                "%s", f"{name:12s} SOL->Token->USDC->SOL: {profit_pct:+.4f}% [{status}]"
            )

            if profit_pct > 0.1:  # More than 0.1% would be noteworthy
                opportunities.append(
                    {
                        "pair": name,
                        "profit_pct": profit_pct,
                        "legs": {
                            "sol_to_token": f"0.0001 SOL -> {sol_to_token} tokens",
                            "token_to_usdc": f"{sol_to_token} tokens -> ${token_to_usdc:.6f}",
                            "usdc_to_sol": f"${token_to_usdc:.6f} -> {usdc_to_sol:.6f} SOL",
                        },
                    }
                )

        except Exception as e:
            logging.debug("%s", f"{name:12s} Error: {str(e)[:40]}")

    if opportunities:
        logging.debug(
            "%s", f"\nFound {len(opportunities)} triangular arb opportunities!"
        )
        for o in opportunities:
            logging.debug(
                "%s", f"  {o['pair']}: {o['profit_pct']:.4f}% potential profit"
            )
    else:
        logging.debug("%s", "\nNo triangular arbitrage found (market is efficient)")

    return opportunities


if __name__ == "__main__":
    detect_triangular_arb()
