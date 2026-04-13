"""
Jupiter Sentinel - Live Trader
Main entry point for autonomous token discovery and strategy execution.
"""

from typing import Any
import argparse
import logging
import time

from src import autotrader
from src import profit_locker
from src import strategies
from src import token_discovery
from src.oracle import PriceFeed
from src.security import sanitize_sensitive_text

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("LiveTrader")


def main() -> Any:
    """Function docstring."""
    parser = argparse.ArgumentParser(description="Live Trader for Jupiter Sentinel")
    parser.add_argument(
        "--live", action="store_true", help="Run in live mode (disables dry-run)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Scan interval in seconds (default: 300 / 5 min)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Number of loop iterations (default: infinite)",
    )
    args = parser.parse_args()

    dry_run = not args.live
    logger.info("Initializing Live Trader...")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f"Interval: {args.interval} seconds")

    # 1. Discover trending tokens every 5 min
    discovery = token_discovery.TokenDiscovery()

    # 3. Executes trades via autotrader
    trader = autotrader.AutoTrader(dry_run=dry_run, scan_interval_secs=args.interval)
    trader.state_manager.start_autosave(
        lambda: trader.state_manager.save_trader_state(trader)
    )

    active_feeds = {}
    iteration = 0

    try:
        while True:
            if args.iterations is not None and iteration >= args.iterations:
                logger.info(f"Reached {args.iterations} iterations. Stopping.")
                break

            logger.info(f"--- Cycle {iteration + 1} ---")
            try:
                # Step 1: Discover trending tokens
                logger.info("1) Discovering trending tokens...")
                pairs = discovery.build_scan_pairs(limit=20)
                logger.info(f"Discovered {len(pairs)} trending pairs.")

                # Keep active feeds updated and track current ones
                current_feeds = []
                for input_mint, output_mint, pair_name in pairs:
                    if pair_name not in active_feeds:
                        feed = PriceFeed(
                            pair_name=pair_name,
                            input_mint=input_mint,
                            output_mint=output_mint,
                        )
                        active_feeds[pair_name] = feed
                        # Trader scanner also needs the feed for executing trades properly
                        if pair_name not in trader._feed_by_pair:
                            trader.scanner.feeds.append(feed)
                            trader._feed_by_pair[pair_name] = feed
                    current_feeds.append(active_feeds[pair_name])

                # Update history on currently discovered feeds
                logger.info("Fetching latest prices for discovered feeds...")
                for feed in current_feeds:
                    feed.fetch_price()

                # Step 2: Run strategies on discovered tokens
                logger.info("2) Running strategies on discovered tokens...")

                # Execute both mean reversion and momentum strategies
                mr_signals = strategies.scan_for_signals(current_feeds)
                mo_signals = strategies.scan_momentum_signals(current_feeds)
                all_signals = mr_signals + mo_signals

                logger.info(f"Generated {len(all_signals)} strategy signals.")

                # Step 3: Execute trades via autotrader
                logger.info("3) Executing trades via autotrader...")
                for sig in all_signals:
                    alert = {
                        "pair": sig["pair"],
                        "direction": sig["direction"],
                        "change_pct": sig.get(
                            "deviation_pct", sig.get("cumulative_change_pct", 1.0)
                        ),
                        "price": sig["price"],
                        "strategy": sig["strategy"],
                    }
                    # AutoTrader will evaluate and open position if eligible
                    trader._handle_alert(alert)

                # Step 4: Lock profits after each close
                logger.info("4) Managing open positions & locking profits...")
                # monitor_positions checks exits, triggers close_position, which natively uses StateManager to lock_profit.
                closed_actions = trader.monitor_positions()
                if closed_actions:
                    logger.info(f"Closed {len(closed_actions)} positions.")

                # Log the current locked profit directly from the profit_locker module
                locked_balance = profit_locker.get_locked_balance()
                tradable_balance = profit_locker.get_tradable_balance(
                    executor=trader.executor
                )
                logger.info(
                    f"Total locked profit: {locked_balance:.6f} SOL. Tradable balance: {tradable_balance:.6f} SOL."
                )

                # Step 5: Log everything
                logger.info("5) Cycle complete. Logging status...")
                open_positions = [
                    p for p in trader.risk_manager.positions if p.status == "open"
                ]
                logger.info(f"Currently open positions: {len(open_positions)}")
            except Exception as exc:
                logger.error(
                    "Cycle failed; state preserved and loop will continue. %s",
                    sanitize_sensitive_text(exc),
                )
            finally:
                trader.save_state()

            iteration += 1
            if args.iterations is not None and iteration >= args.iterations:
                break

            logger.info(f"Sleeping for {args.interval} seconds...")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Stopping...")
    finally:
        logger.info("Shutting down AutoTrader...")
        trader.stop()
        logger.info("Live Trader stopped.")


if __name__ == "__main__":
    main()
