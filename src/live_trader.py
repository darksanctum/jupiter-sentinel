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
from src.config import (
    DEFAULT_LIVE_TRADER_INTERVAL_SECS,
    DEFAULT_LIVE_TRADER_PAIR_LIMIT,
)
from src.jupiter_limits import build_free_tier_bot_config
from src.oracle import PriceFeed
from src.security import sanitize_sensitive_text

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("LiveTrader")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the live trader loop."""
    parser = argparse.ArgumentParser(description="Live Trader for Jupiter Sentinel")
    parser.add_argument(
        "--live", action="store_true", help="Run in live mode (disables dry-run)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_LIVE_TRADER_INTERVAL_SECS,
        help=(
            "Scan interval in seconds "
            f"(default: {DEFAULT_LIVE_TRADER_INTERVAL_SECS} / 5 min)"
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Number of loop iterations (default: infinite)",
    )
    return parser


def _sync_discovered_feeds(
    pairs: list[tuple[str, str, str]],
    active_feeds: dict[str, PriceFeed],
    trader: autotrader.AutoTrader,
) -> list[PriceFeed]:
    """Create and register feeds for the currently discovered token pairs."""
    current_feeds: list[PriceFeed] = []
    for input_mint, output_mint, pair_name in pairs:
        feed = active_feeds.get(pair_name)
        if feed is None:
            feed = PriceFeed(
                pair_name=pair_name,
                input_mint=input_mint,
                output_mint=output_mint,
            )
            active_feeds[pair_name] = feed
        if pair_name not in trader._feed_by_pair:
            trader.scanner.feeds.append(feed)
            trader._feed_by_pair[pair_name] = feed
        current_feeds.append(feed)
    return current_feeds


def _refresh_feed_prices(current_feeds: list[PriceFeed]) -> None:
    """Fetch fresh prices for every discovered feed before running strategies."""
    logger.info("Fetching latest prices for discovered feeds...")
    for feed in current_feeds:
        feed.fetch_price()


def _build_strategy_alerts(current_feeds: list[PriceFeed]) -> list[dict[str, Any]]:
    """Run strategy modules and normalize their outputs into AutoTrader alerts."""
    mr_signals = strategies.scan_for_signals(current_feeds)
    mo_signals = strategies.scan_momentum_signals(current_feeds)
    all_signals = mr_signals + mo_signals
    logger.info("Generated %s strategy signals.", len(all_signals))

    alerts: list[dict[str, Any]] = []
    for signal_data in all_signals:
        alerts.append(
            {
                "pair": signal_data["pair"],
                "direction": signal_data["direction"],
                "change_pct": signal_data.get(
                    "deviation_pct", signal_data.get("cumulative_change_pct", 1.0)
                ),
                "price": signal_data["price"],
                "strategy": signal_data["strategy"],
            }
        )
    return alerts


def _log_profit_status(trader: autotrader.AutoTrader) -> None:
    """Log locked and tradable balances after each strategy cycle."""
    locked_balance = profit_locker.get_locked_balance()
    tradable_balance = profit_locker.get_tradable_balance(executor=trader.executor)
    logger.info(
        "Total locked profit: %.6f SOL. Tradable balance: %.6f SOL.",
        locked_balance,
        tradable_balance,
    )


def _run_cycle(
    discovery: token_discovery.TokenDiscovery,
    trader: autotrader.AutoTrader,
    active_feeds: dict[str, PriceFeed],
    effective_pair_limit: int,
) -> None:
    """Execute one discovery, strategy, and execution cycle."""
    logger.info("1) Discovering trending tokens...")
    pairs = discovery.build_scan_pairs(limit=effective_pair_limit)
    logger.info("Discovered %s trending pairs.", len(pairs))

    current_feeds = _sync_discovered_feeds(pairs, active_feeds, trader)
    _refresh_feed_prices(current_feeds)

    logger.info("2) Running strategies on discovered tokens...")
    alerts = _build_strategy_alerts(current_feeds)

    logger.info("3) Executing trades via autotrader...")
    for alert in alerts:
        trader._handle_alert(alert)

    logger.info("4) Managing open positions & locking profits...")
    closed_actions = trader.monitor_positions()
    if closed_actions:
        logger.info("Closed %s positions.", len(closed_actions))

    _log_profit_status(trader)

    open_positions = [p for p in trader.risk_manager.positions if p.status == "open"]
    logger.info("5) Cycle complete. Currently open positions: %s", len(open_positions))


def main() -> Any:
    """Function docstring."""
    args = build_arg_parser().parse_args()

    dry_run = not args.live
    runtime_limits = build_free_tier_bot_config(
        requested_scan_pairs=DEFAULT_LIVE_TRADER_PAIR_LIMIT,
        requested_scan_interval_seconds=args.interval,
        quote_requests_per_pair=1,
    )
    effective_interval = runtime_limits.effective_scan_interval_seconds
    effective_pair_limit = (
        runtime_limits.max_pairs_per_scan or DEFAULT_LIVE_TRADER_PAIR_LIMIT
    )

    logger.info("Initializing Live Trader...")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(
        "Interval: requested %s seconds, effective %s seconds",
        args.interval,
        effective_interval,
    )
    logger.info("Max Jupiter pairs per cycle on free tier: %s", effective_pair_limit)

    # 1. Discover trending tokens every 5 min
    discovery = token_discovery.TokenDiscovery()

    # 3. Executes trades via autotrader
    trader = autotrader.AutoTrader(
        dry_run=dry_run, scan_interval_secs=effective_interval
    )
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
                _run_cycle(discovery, trader, active_feeds, effective_pair_limit)
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

            logger.info(f"Sleeping for {effective_interval} seconds...")
            time.sleep(effective_interval)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Stopping...")
    finally:
        logger.info("Shutting down AutoTrader...")
        trader.stop()
        logger.info("Live Trader stopped.")


if __name__ == "__main__":
    main()
