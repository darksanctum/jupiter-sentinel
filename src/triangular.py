"""
Jupiter Sentinel - Cross-Route Arbitrage Detector
Detects price discrepancies between SOL->Token and Token->SOL routes.
"""
import json
import urllib.request
from typing import Any, Dict, List
from datetime import datetime

from .config import JUPITER_SWAP_V1, HEADERS, SOL_MINT, USDC_MINT, SCAN_PAIRS


def detect_triangular_arb() -> List[Dict[str, Any]]:
    """
    Detect triangular arbitrage: SOL -> Token -> USDC -> SOL
    If the product of the three exchange rates > 1, there's profit.
    
    This uses Jupiter's swap engine creatively: we quote three legs
    of a triangle and check if the loop is profitable.
    """
    print("Triangular Arbitrage Scanner")
    print("=" * 50)
    
    # Get SOL/USDC rate
    try:
        url = f"{JUPITER_SWAP_V1}/quote?inputMint={SOL_MINT}&outputMint={USDC_MINT}&amount=1000000&slippageBps=10"
        req = urllib.request.Request(url, headers=HEADERS)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        sol_usdc = int(resp["outAmount"]) / 1e6 / 0.001  # USDC per SOL
    except:
        print("Could not get SOL/USDC rate")
        return []
    
    print(f"SOL/USDC: ${sol_usdc:.2f}")
    print()
    
    opportunities: List[Dict[str, Any]] = []
    
    for input_mint, output_mint, name in SCAN_PAIRS:
        if name == "SOL/USDC":
            continue
        
        token_mint = input_mint if input_mint != SOL_MINT else output_mint
        if token_mint == SOL_MINT or token_mint == USDC_MINT:
            continue
            
        try:
            # Leg 1: SOL -> Token (100k lamports)
            url1 = f"{JUPITER_SWAP_V1}/quote?inputMint={SOL_MINT}&outputMint={token_mint}&amount=100000&slippageBps=10"
            req1 = urllib.request.Request(url1, headers=HEADERS)
            resp1 = json.loads(urllib.request.urlopen(req1, timeout=10).read())
            sol_to_token = int(resp1["outAmount"])
            
            # Leg 2: Token -> USDC
            url2 = f"{JUPITER_SWAP_V1}/quote?inputMint={token_mint}&outputMint={USDC_MINT}&amount={sol_to_token}&slippageBps=10"
            req2 = urllib.request.Request(url2, headers=HEADERS)
            resp2 = json.loads(urllib.request.urlopen(req2, timeout=10).read())
            token_to_usdc = int(resp2["outAmount"]) / 1e6
            
            # Leg 3: USDC -> SOL (implied)
            usdc_to_sol = token_to_usdc / sol_usdc  # SOL we'd get back
            
            # Check: did we end up with more SOL than we started?
            initial_sol = 0.0001  # 100k lamports
            profit_pct = (usdc_to_sol - initial_sol) / initial_sol * 100
            
            status = "PROFIT" if profit_pct > 0 else "LOSS"
            
            print(f"{name:12s} SOL->Token->USDC->SOL: {profit_pct:+.4f}% [{status}]")
            
            if profit_pct > 0.1:  # More than 0.1% would be noteworthy
                opportunities.append({
                    "pair": name,
                    "profit_pct": profit_pct,
                    "legs": {
                        "sol_to_token": f"0.0001 SOL -> {sol_to_token} tokens",
                        "token_to_usdc": f"{sol_to_token} tokens -> ${token_to_usdc:.6f}",
                        "usdc_to_sol": f"${token_to_usdc:.6f} -> {usdc_to_sol:.6f} SOL",
                    },
                })
                
        except Exception as e:
            print(f"{name:12s} Error: {str(e)[:40]}")
    
    if opportunities:
        print(f"\nFound {len(opportunities)} triangular arb opportunities!")
        for o in opportunities:
            print(f"  {o['pair']}: {o['profit_pct']:.4f}% potential profit")
    else:
        print("\nNo triangular arbitrage found (market is efficient)")
    
    return opportunities


if __name__ == "__main__":
    detect_triangular_arb()
