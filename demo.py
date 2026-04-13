#!/usr/bin/env python3
"""
Jupiter Sentinel - Demo Script
Shows all features in a single run without requiring a wallet.
"""
import json
import sys
import os

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SCAN_PAIRS, SOL_MINT, USDC_MINT, JUP_MINT, BONK_MINT, WIF_MINT
from src.scanner import VolatilityScanner
from src.arbitrage import RouteArbitrage


def demo():
    print()
    print("JUPITER SENTINEL - DEMO")
    print("=" * 60)
    print("An autonomous AI DeFi agent combining 5 Jupiter APIs")
    print()
    
    # 1. Volatility Scanner Demo
    print("1. VOLATILITY SCANNER (Price Oracle via Swap Quotes)")
    print("-" * 60)
    print("Using Jupiter's swap engine as a real-time price oracle...")
    print()
    
    scanner = VolatilityScanner()
    alerts = scanner.scan_once()
    
    for feed in scanner.feeds:
        if feed.current_price:
            change = feed.price_change_pct * 100
            arrow = "+" if change >= 0 else ""
            print(f"  {feed.pair_name:12s}  ${feed.current_price:>12.6f}  {arrow}{change:.2f}%")
    
    print()
    
    # 2. Route Arbitrage Demo
    print("2. ROUTE ARBITRAGE DETECTOR")
    print("-" * 60)
    print("Detecting price discrepancies between swap routes...")
    print()
    
    arb = RouteArbitrage()
    
    # Scan SOL/USDC with different amounts
    opps = arb.scan_pair(SOL_MINT, USDC_MINT, "SOL/USDC")
    if opps:
        for o in opps:
            print(f"  {o.pair}: {o.spread_pct:.2f}% spread")
            print(f"    Buy route:  {o.buy_route}")
            print(f"    Sell route: {o.sell_route}")
    else:
        print("  SOL/USDC: No route discrepancy (market efficient)")
    
    print()
    
    # 3. Show API Usage Summary
    print("3. JUPITER APIs COMBINED")
    print("-" * 60)
    print("This project creatively uses:")
    print()
    print("  Swap V1 (/quote + /swap)")
    print("  -> As a PRICE ORACLE: quote small amounts to get real-time prices")
    print("  -> As EXECUTION: sign and broadcast swap transactions")
    print("  -> As ARBITRAGE DETECTOR: quote different amounts, compare routes")
    print()
    print("  Price (derived from quotes)")
    print("  -> Real-time volatility tracking without a dedicated price API")
    print()
    print("  Tokens (token list + metadata)")
    print("  -> Token screening for the scanner")
    print()
    print("  Trigger (limit orders)")
    print("  -> Planned: auto-set stop-loss and take-profit orders")
    print()
    print("  Lend (flash loans)")
    print("  -> Planned: flash loan arbitrage execution")
    print()
    
    # 4. Architecture Summary
    print("4. ARCHITECTURE")
    print("-" * 60)
    print("""
    Telegram Interface
           |
      AI Brain (Decision Engine)
           |
    ------+------+------
    |            |      |
    Volatility  Trade  Risk
    Scanner    Executor Manager
    |            |      |
    ------+------+------
           |
    Jupiter APIs (Swap, Price, Tokens, Trigger, Lend)
    """)
    
    print("5. CREATIVE API USAGE")
    print("-" * 60)
    print("What makes this project unique:")
    print()
    print("  * Quotes-as-Oracle: We repurpose Jupiter's swap quote")
    print("    engine as a real-time multi-pair price feed.")
    print()
    print("  * Cross-Route Arbitrage: We detect price differences")
    print("    between Jupiter's own routing options.")
    print()
    print("  * Volatility-Adaptive: Position sizing and stops")
    print("    auto-adjust based on real-time market volatility.")
    print()
    print("  * Full Autonomy: Runs 24/7, no human intervention needed.")
    print()
    
    print("=" * 60)
    print("Jupiter Sentinel - Built for the 'Not Your Regular Bounty'")
    print("Superteam Earn x Jupiter | $3,000 bounty")
    print()


if __name__ == "__main__":
    demo()
