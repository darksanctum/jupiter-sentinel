"""
Jupiter Sentinel - Volatility Scanner
Continuously monitors token pairs and detects volatility spikes.
Uses Jupiter's quote engine as a real-time price oracle.
"""

import logging
import time
import json
from datetime import datetime
from typing import Any, Callable, List, Optional

from .config import SCAN_PAIRS, SCAN_INTERVAL_SECS, VOLATILITY_THRESHOLD
from .oracle import PriceFeed


class VolatilityScanner:
    """
    Scans multiple token pairs using Jupiter's swap quote engine
    as a price oracle. Detects volatility spikes in real-time.

    Creative usage: We treat Jupiter's routing engine as a multi-pair
    price feed by repeatedly quoting small amounts across pairs.
    """

    def __init__(self) -> None:
        """Function docstring."""
        self.feeds: List[PriceFeed] = []
        self.alerts: List[dict] = []
        self.running = False

        for input_mint, output_mint, name in SCAN_PAIRS:
            self.feeds.append(
                PriceFeed(
                    pair_name=name,
                    input_mint=input_mint,
                    output_mint=output_mint,
                )
            )

    def scan_once(self) -> List[dict[str, Any]]:
        """Run one scan cycle across all pairs."""
        new_alerts = []
        timestamp = datetime.utcnow().isoformat()

        for feed in self.feeds:
            point = feed.fetch_price()
            if not point:
                continue

            volatility = feed.volatility
            change = feed.price_change_pct

            # Detect volatility spikes
            if abs(change) > VOLATILITY_THRESHOLD and len(feed.history) >= 5:
                alert = {
                    "timestamp": timestamp,
                    "pair": feed.pair_name,
                    "price": point.price,
                    "change_pct": change * 100,
                    "volatility": volatility,
                    "direction": "UP" if change > 0 else "DOWN",
                    "severity": "HIGH" if abs(change) > 0.10 else "MEDIUM",
                }
                new_alerts.append(alert)
                self.alerts.append(alert)

        return new_alerts

    def scan_loop(
        self,
        callback: Optional[Callable[[List[dict[str, Any]]], None]] = None,
        max_iterations: Optional[int] = None,
    ) -> None:
        """
        Continuous scanning loop.

        Args:
            callback: Function called with new alerts each cycle
            max_iterations: Max scan cycles (None = infinite)
        """
        self.running = True
        iteration = 0

        while self.running:
            if max_iterations and iteration >= max_iterations:
                break

            alerts = self.scan_once()

            if alerts and callback:
                callback(alerts)

            # Print status
            self._print_status()

            iteration += 1
            time.sleep(SCAN_INTERVAL_SECS)

    def _print_status(self) -> None:
        """Print current scanner status."""
        now = datetime.utcnow().strftime("%H:%M:%S")
        logging.debug("%s", f"\n[{now}] Scanner Status:")
        for feed in self.feeds:
            if feed.current_price:
                vol_bar = "█" * int(feed.volatility * 100)
                change = feed.price_change_pct * 100
                arrow = "▲" if change >= 0 else "▼"
                logging.debug(
                    "%s",
                    f"  {feed.pair_name:12s} "
                    f"${feed.current_price:>10.4f} "
                    f"{arrow}{abs(change):>5.2f}% "
                    f"vol:{vol_bar:<10s} "
                    f"({feed.volatility:.4f})",
                )

    def stop(self) -> None:
        """Function docstring."""
        self.running = False

    def get_report(self) -> dict[str, Any]:
        """Generate a full scanner report."""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "pairs": [f.stats() for f in self.feeds],
            "total_alerts": len(self.alerts),
            "recent_alerts": self.alerts[-10:],
        }


def run_standalone() -> None:
    """Run scanner in standalone mode."""
    logging.debug("%s", "Jupiter Sentinel - Volatility Scanner")
    logging.debug("%s", "=" * 50)
    logging.debug("%s", f"Monitoring {len(SCAN_PAIRS)} pairs")
    logging.debug("%s", f"Alert threshold: {VOLATILITY_THRESHOLD*100}% price change")
    logging.debug("%s", f"Scan interval: {SCAN_INTERVAL_SECS}s")
    logging.debug("")

    scanner = VolatilityScanner()

    def on_alert(alerts: List[dict[str, Any]]) -> None:
        """Function docstring."""
        for a in alerts:
            emoji = "🚀" if a["direction"] == "UP" else "⚠️"
            logging.debug(
                "%s",
                f"\n{emoji} ALERT: {a['pair']} {a['direction']} "
                f"{abs(a['change_pct']):.2f}% "
                f"@ ${a['price']:.4f} [{a['severity']}]",
            )

    try:
        scanner.scan_loop(callback=on_alert)
    except KeyboardInterrupt:
        scanner.stop()
        logging.debug("%s", "\nScanner stopped.")


if __name__ == "__main__":
    run_standalone()
