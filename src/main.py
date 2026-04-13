"""
Jupiter Sentinel - Main Entry Point
Autonomous AI DeFi agent that combines multiple Jupiter APIs.
"""
import time
import json
import signal
import sys
from datetime import datetime

from .config import SCAN_PAIRS
from .scanner import VolatilityScanner
from .executor import TradeExecutor
from .risk import RiskManager
from .arbitrage import RouteArbitrage
from .sentiment import SentimentAnalyzer


class JupiterSentinel:
    """
    The main orchestrator that ties together:
    - Volatility Scanner (Price Oracle)
    - Trade Executor (Swap V1)
    - Risk Manager (Stop-loss, Position sizing)
    - Route Arbitrage Detector
    """
    
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.scanner = VolatilityScanner()
        self.executor = TradeExecutor()
        self.risk_manager = RiskManager(self.executor)
        self.arbitrage = RouteArbitrage()
        self.sentiment = SentimentAnalyzer()
        self.running = False
        self.cycle = 0
    
    def start(self):
        """Start the sentinel agent."""
        print()
        print("JUPITER SENTINEL")
        print("=" * 50)
        print("Autonomous AI DeFi Agent")
        print()
        
        # Show wallet info
        balance = self.executor.get_balance()
        print(f"Wallet: {balance['address']}")
        print(f"Balance: {balance['sol']:.6f} SOL (${balance['usd_value']:.2f})")
        print(f"SOL Price: ${balance['sol_price']:.2f}")
        print()
        
        if self.dry_run:
            print("MODE: DRY RUN (no real trades)")
        else:
            print("MODE: LIVE TRADING")
        print()
        
        print(f"Monitoring: {', '.join(p[2] for p in SCAN_PAIRS)}")
        print()
        
        # Run arbitrage scan first
        print("Running initial arbitrage scan...")
        arb_report = self.arbitrage.scan_all(SCAN_PAIRS)
        if arb_report["opportunities"]:
            for opp in arb_report["opportunities"]:
                print(f"  {opp['pair']}: {opp['spread']} spread")
        else:
            print("  No route arbitrage detected (market efficient)")
        print()
        
        # Start scanning
        print("Starting volatility scanner...")
        self.running = True
        
        def on_alert(alerts):
            for a in alerts:
                self._handle_alert(a)
        
        try:
            self.scanner.scan_loop(callback=on_alert)
        except KeyboardInterrupt:
            self.shutdown()
    
    def _handle_alert(self, alert: dict):
        """Handle a volatility alert from the scanner."""
        self.cycle += 1
        
        print()
        print(f"{'='*50}")
        print(f"ALERT #{self.cycle} | {alert['timestamp'][:19]}")
        print(f"  {alert['pair']} {alert['direction']} {abs(alert['change_pct']):.2f}%")
        print(f"  Price: ${alert['price']:.4f} | Severity: {alert['severity']}")
        
        # Check for arbitrage on this pair
        for input_mint, output_mint, name in SCAN_PAIRS:
            if name == alert["pair"]:
                opps = self.arbitrage.scan_pair(input_mint, output_mint, name)
                for o in opps:
                    print(f"  ARB: {o.spread_pct:.2f}% spread via {o.buy_route}")
                break
        
        # Risk check existing positions
        actions = self.risk_manager.check_positions()
        for a in actions:
            print(f"  RISK: {a['type']} on {a['pair']} | PnL: {a['pnl_pct']:+.2f}%")
        
        # AI decision: should we trade?
        if alert["severity"] == "HIGH" and abs(alert["change_pct"]) > 5:
            if alert["direction"] == "DOWN" and not self.dry_run:
                if self.sentiment.is_extreme_fear():
                    print(f"  DECISION: AVOID BUY - market is in extreme fear")
                else:
                    # Buy the dip (contrarian)
                    print(f"  DECISION: BUY (contrarian) - price dropped significantly")
            elif alert["direction"] == "UP" and not self.dry_run:
                print(f"  DECISION: WATCH - momentum spike, waiting for pullback")
            else:
                print(f"  DECISION: OBSERVE - collecting data")
        
        print(f"{'='*50}")
    
    def shutdown(self):
        """Graceful shutdown with report."""
        self.running = False
        self.scanner.stop()
        
        print()
        print("SENTINEL SHUTDOWN")
        print("=" * 50)
        
        # Final report
        report = self.risk_manager.get_portfolio_report()
        scanner_report = self.scanner.get_report()
        
        print(f"Wallet: {report['wallet']['sol']:.6f} SOL (${report['wallet']['usd_value']:.2f})")
        print(f"Total alerts: {scanner_report['total_alerts']}")
        print(f"Total trades: {report['total_trades']}")
        print(f"Open positions: {len(report['open_positions'])}")
        print(f"Arb opportunities found: {len(self.arbitrage.opportunities)}")
        
        # Save reports
        from .config import DATA_DIR
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        
        with open(DATA_DIR / f"report_{ts}.json", "w") as f:
            json.dump({
                "portfolio": report,
                "scanner": scanner_report,
                "arbitrage": len(self.arbitrage.opportunities),
            }, f, indent=2, default=str)
        
        print(f"\nReport saved to data/report_{ts}.json")


def main():
    """Main entry point."""
    dry_run = "--live" not in sys.argv
    sentinel = JupiterSentinel(dry_run=dry_run)
    
    def sig_handler(sig, frame):
        sentinel.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, sig_handler)
    sentinel.start()


if __name__ == "__main__":
    main()
