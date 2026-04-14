"""
Jupiter Sentinel - Autonomous Trader
Wires the scanner, risk manager, executor, and oracle into
one persistent trading loop with restart recovery.
"""

import logging
import argparse
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Optional

from .config import (
    DATA_DIR,
    MAX_POSITION_USD,
    SCAN_INTERVAL_SECS,
    SCAN_PAIRS,
    SOL_MINT,
    USDC_MINT,
    VOLATILE_MARKET_STOP_LOSS_PCT,
    VOLATILE_MARKET_TAKE_PROFIT_PCT,
)
from .correlation_tracker import CorrelationTracker
from .executor import TradeExecutor
from .oracle import PriceFeed
from .regime_detector import RegimeDetector, MarketRegime
from .resilience import has_reconcilable_transactions, reconcile_transaction_state
from .risk import Position, RiskManager
from .scanner import VolatilityScanner
from .security import display_wallet_status, sanitize_sensitive_text
from .state_manager import StateManager

SUCCESS_STATUSES = {"success", "dry_run"}


@dataclass(frozen=True)
class EntryContext:
    pair: str
    scan_input_mint: str
    scan_output_mint: str
    held_mint: str
    shared_feed: PriceFeed
    regime: MarketRegime
    stop_loss_pct: Optional[float]
    take_profit_pct: Optional[float]


class AutoTrader:
    """
    Persistent autonomous trader driven by volatility alerts.

    Flow:
    1. Scanner detects a volatile opportunity.
    2. Risk manager sizes and approves the position.
    3. Executor performs the buy.
    4. Monitor checks every open position.
    5. Executor auto-sells when the risk manager signals an exit.
    """

    def __init__(
        self,
        *,
        dry_run: bool = True,
        entry_amount_sol: float = 0.25,
        enter_on: str = "down",
        max_open_positions: Optional[int] = None,
        scan_interval_secs: int = SCAN_INTERVAL_SECS,
        state_path: Path | str = DATA_DIR / "state.json",
        scanner: Optional[VolatilityScanner] = None,
        executor: Optional[TradeExecutor] = None,
        risk_manager: Optional[RiskManager] = None,
        correlation_tracker: Optional[CorrelationTracker] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Function docstring."""
        if enter_on not in {"down", "up", "all"}:
            raise ValueError("enter_on must be one of: down, up, all")
        if entry_amount_sol <= 0:
            raise ValueError("entry_amount_sol must be positive")
        if scan_interval_secs <= 0:
            raise ValueError("scan_interval_secs must be positive")
        if max_open_positions is not None and max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive when provided")

        self.dry_run = dry_run
        self.entry_amount_sol = float(entry_amount_sol)
        self.enter_on = enter_on
        self.max_open_positions = max_open_positions
        self.scan_interval_secs = int(scan_interval_secs)
        self.sleep_fn = sleep_fn

        self.state_path = Path(state_path).expanduser()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_manager = StateManager(self.state_path, logger=self._log)

        self.scanner = scanner or VolatilityScanner()
        self.executor = executor or TradeExecutor()
        self.risk_manager = risk_manager or RiskManager(self.executor)
        self.correlation_tracker = correlation_tracker or CorrelationTracker(
            path=DATA_DIR / "correlations.json"
        )
        self.regime_detector = RegimeDetector()
        setattr(self.risk_manager, "state_path", self.state_path)

        self.running = False
        self.cycle = 0
        self.position_meta: dict[str, dict[str, Any]] = {}
        self.started_at = time.time()
        self.last_cycle_started_at: Optional[float] = None
        self.last_successful_cycle_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None
        self.pair_lookup = {
            name: (input_mint, output_mint)
            for input_mint, output_mint, name in SCAN_PAIRS
        }
        self._feed_by_pair: dict[str, PriceFeed] = {}
        self._index_scanner_feeds()
        state = self.state_manager.load_into_trader(self)
        self._reconcile_startup_state(state)

    def run(self, max_iterations: Optional[int] = None) -> None:
        """Start the continuous trading loop."""
        self._reset_runtime_state()
        iteration = 0
        self.state_manager.start_autosave(
            lambda: self.state_manager.save_trader_state(self)
        )
        self._log_startup_state()

        try:
            while self.running:
                if self._should_stop_for_iteration(iteration, max_iterations):
                    break
                self._run_cycle()
                iteration += 1
                if self._should_stop_for_iteration(iteration, max_iterations):
                    break
                self.sleep_fn(self.scan_interval_secs)
        except KeyboardInterrupt:
            self._log("Keyboard interrupt received")
        finally:
            self.stop()

    def _reset_runtime_state(self) -> None:
        """Reset transient runtime health fields before starting the loop."""
        self.running = True
        self.started_at = time.time()
        self.last_cycle_started_at = None
        self.last_successful_cycle_at = None
        self.last_error = None
        self.last_error_at = None

    def _log_startup_state(self) -> None:
        """Emit startup configuration and recovered runtime state."""
        self._log("AUTO TRADER START")
        self._log(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        self._log(f"State file: {self.state_path}")
        self._log(f"Entry size: {self.entry_amount_sol:.6f} SOL")
        self._log(f"Enter on: {self.enter_on}")
        if self.max_open_positions is not None:
            self._log(f"Max open positions: {self.max_open_positions}")

        balance = self.executor.get_balance()
        self._log(
            f"Wallet: {display_wallet_status(balance.get('address', 'unknown'))} | "
            f"{balance.get('sol', 0.0):.6f} SOL (${balance.get('usd_value', 0.0):.2f})"
        )
        if self.risk_manager.positions:
            self._log(
                f"Recovered {len(self.risk_manager.positions)} open position(s) from state"
            )

    @staticmethod
    def _should_stop_for_iteration(
        iteration: int, max_iterations: Optional[int]
    ) -> bool:
        """Return True when the caller reached the optional iteration cap."""
        return max_iterations is not None and iteration >= max_iterations

    def _run_cycle(self) -> None:
        """Execute one trading cycle and capture runtime health state."""
        self.cycle += 1
        self.last_cycle_started_at = time.time()
        self._log(f"Cycle {self.cycle}")

        try:
            self.monitor_positions()
            alerts = self.scanner.scan_once()
            self.correlation_tracker.refresh_if_due(self._feed_by_pair)
            for alert in alerts:
                self._handle_alert(alert)
            self.save_state()
            self.last_successful_cycle_at = time.time()
            self.last_error = None
            self.last_error_at = None
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            self.last_error_at = time.time()
            self._log(f"Cycle error: {exc}")
            self.save_state()

    def stop(self) -> None:
        """Stop the loop and persist the latest state."""
        self.state_manager.stop_autosave()

        if not self.running:
            self.scanner.stop()
            self.save_state()
            return

        self.running = False
        self.scanner.stop()
        self.save_state()
        self._log("AUTO TRADER STOP")

    def monitor_positions(self) -> list[dict[str, Any]]:
        """Check open positions and auto-sell when exits trigger."""
        actions = self.risk_manager.check_positions()
        for action in actions:
            self._close_position(action)
        return actions

    def save_state(self) -> None:
        """Atomically persist the runtime state to disk."""
        self.state_manager.save_trader_state(self)

    def _handle_alert(self, alert: dict[str, Any]) -> None:
        """Evaluate a scanner alert and open a position when eligible."""
        pair = str(alert.get("pair", ""))
        direction = str(alert.get("direction", "")).upper()
        change_pct = float(alert.get("change_pct", 0.0) or 0.0)

        self._log(
            f"Alert {pair} {direction} {change_pct:+.2f}% @ ${float(alert.get('price', 0.0) or 0.0):.6f}"
        )

        if self._has_open_position(pair):
            self._log(f"Skipping {pair}: position already open")
            return

        open_positions = self._open_positions()
        if self._max_open_positions_reached(open_positions):
            self._log(f"Skipping {pair}: max open positions reached")
            return

        if not self._should_enter_for_direction(direction):
            return

        context = self._resolve_entry_context(pair, open_positions)
        if context is None:
            return

        position = self._open_entry_position(context)
        if position is None:
            self._log(f"Risk manager rejected {pair}")
            return
        if float(getattr(position, "notional", 0.0) or 0.0) > MAX_POSITION_USD + 1e-9:
            self._rollback_open_position(pair, position)
            self._log(
                f"Blocking {pair}: hard position limit exceeded "
                f"(${float(position.notional):.2f} > ${MAX_POSITION_USD:.2f})"
            )
            return

        self.risk_manager.price_feeds[pair] = context.shared_feed
        self._complete_entry(alert, context, position)

    def _open_positions(self) -> list[Position]:
        """Return the current open positions tracked by the risk manager."""
        return [
            position
            for position in self.risk_manager.positions
            if position.status == "open"
        ]

    def _has_open_position(self, pair: str) -> bool:
        """Check whether the pair already has an open or pending local position."""
        return pair in self.position_meta or any(
            position.pair == pair and position.status == "open"
            for position in self.risk_manager.positions
        )

    def _max_open_positions_reached(self, open_positions: list[Position]) -> bool:
        """Check whether the configured open-position cap was hit."""
        return (
            self.max_open_positions is not None
            and len(open_positions) >= self.max_open_positions
        )

    def _should_enter_for_direction(self, direction: str) -> bool:
        """Validate whether the current alert direction matches the strategy mode."""
        if self.enter_on == "down":
            return direction == "DOWN"
        if self.enter_on == "up":
            return direction == "UP"
        return True

    def _resolve_entry_context(
        self, pair: str, open_positions: list[Position]
    ) -> Optional[EntryContext]:
        """Resolve all state needed to attempt a new position entry."""
        pair_config = self._resolve_pair(pair)
        if pair_config is None:
            self._log(f"Skipping {pair}: pair is not configured")
            return None

        scan_input_mint, scan_output_mint = pair_config
        held_mint = self._derive_held_mint(scan_input_mint, scan_output_mint)
        if held_mint is None:
            self._log(f"Skipping {pair}: no non-SOL asset to trade for this pair")
            return None

        self.correlation_tracker.refresh_if_due(self._feed_by_pair)
        conflict = self.correlation_tracker.find_correlated_open_position(
            pair,
            scan_input_mint,
            scan_output_mint,
            open_positions,
            pair_lookup=self.pair_lookup,
            position_meta=self.position_meta,
        )
        if conflict is not None:
            self._log(
                f"Skipping {pair}: correlation {float(conflict['correlation']):.2f} "
                f"with open {conflict['open_pair']} exceeds "
                f"{self.correlation_tracker.threshold:.2f}"
            )
            return None

        shared_feed = self._ensure_scanner_feed(pair, scan_input_mint, scan_output_mint)
        regime = self.regime_detector.detect(shared_feed)
        if regime == MarketRegime.BEAR:
            self._log(f"Skipping {pair}: market regime is BEAR (no longs)")
            return None

        stop_loss_pct, take_profit_pct = self._risk_parameters_for_regime(pair, regime)
        return EntryContext(
            pair=pair,
            scan_input_mint=scan_input_mint,
            scan_output_mint=scan_output_mint,
            held_mint=held_mint,
            shared_feed=shared_feed,
            regime=regime,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )

    def _risk_parameters_for_regime(
        self, pair: str, regime: MarketRegime
    ) -> tuple[Optional[float], Optional[float]]:
        """Return regime-specific risk overrides for a candidate entry."""
        if regime != MarketRegime.VOLATILE:
            return None, None

        self._log(f"Market is VOLATILE for {pair}, using wider stops")
        return (
            VOLATILE_MARKET_STOP_LOSS_PCT,
            VOLATILE_MARKET_TAKE_PROFIT_PCT,
        )

    def _open_entry_position(self, context: EntryContext) -> Optional[Position]:
        """Ask the risk manager to size and validate a new position."""
        return self.risk_manager.open_position(
            pair=context.pair,
            input_mint=context.scan_input_mint,
            output_mint=context.scan_output_mint,
            amount_sol=self.entry_amount_sol,
            stop_loss_pct=context.stop_loss_pct,
            take_profit_pct=context.take_profit_pct,
            dry_run=True,
        )

    def _complete_entry(
        self,
        alert: dict[str, Any],
        context: EntryContext,
        position: Position,
    ) -> None:
        """Execute the entry swap and persist local metadata on success."""
        pair = context.pair
        entry_amount_lamports = int(position.amount_sol * 1e9)
        entry_result = self.executor.execute_swap(
            input_mint=SOL_MINT,
            output_mint=context.held_mint,
            amount=entry_amount_lamports,
            dry_run=self.dry_run,
        )

        if entry_result.get("status") not in SUCCESS_STATUSES:
            self._rollback_open_position(pair, position)
            self._log(
                f"Entry failed for {pair}: {entry_result.get('error', 'unknown error')}"
            )
            return

        entry_amount_units = int(entry_result.get("out_amount", 0) or 0)
        if entry_amount_units <= 0:
            self._rollback_open_position(pair, position)
            self._log(f"Entry failed for {pair}: quote returned no output amount")
            return

        position.tx_buy = entry_result.get("tx_signature")
        position.notional = float(entry_result.get("out_usd", 0.0) or 0.0)
        self.position_meta[pair] = {
            "held_mint": context.held_mint,
            "scan_input_mint": context.scan_input_mint,
            "scan_output_mint": context.scan_output_mint,
            "entry_amount_units": entry_amount_units,
            "entry_amount_lamports": entry_amount_lamports,
            "entry_alert": dict(alert),
            "entry_result": dict(entry_result),
            "opened_at": datetime.utcnow().isoformat(),
        }
        self._log(
            f"Opened {pair} | {position.amount_sol:.6f} SOL -> {entry_amount_units} units "
            f"[{entry_result.get('status')}]"
        )
        self.save_state()

    def _close_position(self, action: dict[str, Any]) -> None:
        """Execute the exit swap for a position the risk manager wants closed."""
        pair = str(action.get("pair", ""))
        meta = self.position_meta.get(pair)
        closed_record = self._find_closed_record(pair)

        if meta is None:
            if closed_record is not None:
                self._restore_closed_position(closed_record)
            self._log(f"Exit triggered for {pair}, but no position metadata was found")
            return
        if closed_record is None:
            self._log(
                f"Exit triggered for {pair}, but no closed position record was found"
            )
            return

        amount_units = int(meta.get("entry_amount_units", 0) or 0)
        if amount_units <= 0:
            self._log(f"Exit blocked for {pair}: missing token amount to sell")
            self._restore_closed_position(closed_record)
            return

        exit_result = self.executor.execute_swap(
            input_mint=str(meta["held_mint"]),
            output_mint=SOL_MINT,
            amount=amount_units,
            dry_run=self.dry_run,
        )

        if exit_result.get("status") not in SUCCESS_STATUSES:
            closed_record["exit_result"] = dict(exit_result)
            self._restore_closed_position(closed_record)
            self._log(
                f"Exit failed for {pair}: {exit_result.get('error', 'unknown error')}"
            )
            return

        closed_record["meta"] = dict(meta)
        closed_record["entry_result"] = dict(meta.get("entry_result", {}))
        closed_record["exit_result"] = dict(exit_result)

        entry_amount_lamports = int(meta.get("entry_amount_lamports", 0) or 0)
        exit_amount_lamports = int(exit_result.get("out_amount", 0) or 0)
        realized_profit_sol = max(
            (exit_amount_lamports - entry_amount_lamports) / 1e9, 0.0
        )
        if realized_profit_sol > 0:
            closed_record["realized_profit_sol"] = realized_profit_sol
            if not self.dry_run:
                locked_profit_sol = self.state_manager.lock_profit(realized_profit_sol)
                closed_record["locked_profit_sol"] = locked_profit_sol
                self._log(
                    f"Locked {locked_profit_sol:.6f} SOL from realized profit on {pair}"
                )

        self.position_meta.pop(pair, None)
        self._log(
            f"Closed {pair} via {action.get('type', 'EXIT')} | "
            f"PnL {float(action.get('pnl_pct', 0.0) or 0.0):+.2f}% "
            f"[{exit_result.get('status')}]"
        )
        self.save_state()

    def _rollback_open_position(self, pair: str, position: Position) -> None:
        """Function docstring."""
        if position in self.risk_manager.positions:
            self.risk_manager.positions.remove(position)
        self.risk_manager.price_feeds.pop(pair, None)
        self.position_meta.pop(pair, None)

    def _restore_closed_position(self, closed_record: dict[str, Any]) -> None:
        """Re-open a position locally if the sell transaction failed."""
        position = closed_record.get("position")
        if not isinstance(position, Position):
            return

        position.status = "open"
        if position not in self.risk_manager.positions:
            self.risk_manager.positions.append(position)

        meta = self.position_meta.get(position.pair, {})
        scan_input_mint = str(meta.get("scan_input_mint", position.input_mint))
        scan_output_mint = str(meta.get("scan_output_mint", position.output_mint))
        self.risk_manager.price_feeds[position.pair] = self._ensure_scanner_feed(
            position.pair,
            scan_input_mint,
            scan_output_mint,
        )
        try:
            self.risk_manager.closed_positions.remove(closed_record)
        except ValueError:
            pass

    def _find_closed_record(self, pair: str) -> Optional[dict[str, Any]]:
        """Function docstring."""
        for record in reversed(self.risk_manager.closed_positions):
            position = record.get("position")
            if isinstance(position, Position) and position.pair == pair:
                return record
        return None

    def _index_scanner_feeds(self) -> None:
        """Function docstring."""
        self._feed_by_pair = {feed.pair_name: feed for feed in self.scanner.feeds}

    def _ensure_scanner_feed(
        self, pair: str, input_mint: str, output_mint: str
    ) -> PriceFeed:
        """Function docstring."""
        feed = self._feed_by_pair.get(pair)
        if feed is not None:
            return feed

        feed = PriceFeed(pair_name=pair, input_mint=input_mint, output_mint=output_mint)
        self.scanner.feeds.append(feed)
        self._feed_by_pair[pair] = feed
        return feed

    def _resolve_pair(self, pair: str) -> Optional[tuple[str, str]]:
        """Function docstring."""
        if pair in self.pair_lookup:
            return self.pair_lookup[pair]

        meta = self.position_meta.get(pair, {})
        input_mint = meta.get("scan_input_mint")
        output_mint = meta.get("scan_output_mint")
        if isinstance(input_mint, str) and isinstance(output_mint, str):
            return input_mint, output_mint
        return None

    def _derive_held_mint(self, input_mint: str, output_mint: str) -> Optional[str]:
        """Function docstring."""
        if input_mint not in {SOL_MINT, USDC_MINT}:
            return input_mint
        if output_mint not in {SOL_MINT, USDC_MINT}:
            return output_mint
        return None

    def _reconcile_startup_state(self, state: dict[str, Any]) -> None:
        """Function docstring."""
        if not has_reconcilable_transactions(state):
            return

        try:
            reconciliation = reconcile_transaction_state(state, logger=self._log)
        except Exception as exc:
            self._log(f"Startup reconciliation skipped: {exc}")
            return

        transactions = reconciliation.get("transactions", [])
        if transactions:
            pending_count = sum(1 for tx in transactions if tx.status == "pending")
            failed_count = sum(1 for tx in transactions if tx.status == "failed")
            self._log(
                f"Startup reconciliation checked {len(transactions)} transaction(s): "
                f"{pending_count} pending, {failed_count} failed"
            )

        if reconciliation.get("changed"):
            self.state_manager.save(reconciliation["state"])
            self.state_manager.load_into_trader(self)

    def _log(self, message: str) -> None:
        """Function docstring."""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        logging.debug("%s", f"[{timestamp}] {sanitize_sensitive_text(message)}")

    @staticmethod
    def _format_health_timestamp(timestamp: Optional[float]) -> Optional[str]:
        """Convert a POSIX timestamp into an RFC3339-style UTC string."""
        if timestamp is None:
            return None
        return datetime.utcfromtimestamp(timestamp).isoformat(timespec="seconds") + "Z"

    def get_health_snapshot(
        self, stale_after_secs: float = 180.0
    ) -> tuple[int, dict[str, Any]]:
        """Return an HTTP status code and JSON payload for runtime health checks."""
        now = time.time()
        open_positions = self._open_positions()
        status, http_status = self._determine_health_status(now, stale_after_secs)

        payload = {
            "service": "jupiter-sentinel",
            "status": status,
            "healthy": http_status == 200,
            "mode": "dry-run" if self.dry_run else "live",
            "cycle": self.cycle,
            "scan_interval_secs": self.scan_interval_secs,
            "stale_after_secs": stale_after_secs,
            "started_at": self._format_health_timestamp(self.started_at),
            "last_cycle_started_at": self._format_health_timestamp(
                self.last_cycle_started_at
            ),
            "last_successful_cycle_at": self._format_health_timestamp(
                self.last_successful_cycle_at
            ),
            "last_error_at": self._format_health_timestamp(self.last_error_at),
            "last_error": self.last_error,
            "uptime_seconds": round(max(now - self.started_at, 0.0), 3),
            "state_file": str(self.state_path.resolve()),
            "open_positions": len(open_positions),
            "closed_positions": len(self.risk_manager.closed_positions),
        }
        return http_status, payload

    def _determine_health_status(
        self, now: float, stale_after_secs: float
    ) -> tuple[str, int]:
        """Resolve the current health label and HTTP status code."""
        if not self.running:
            return "stopped", 503

        if self.last_successful_cycle_at is None:
            if self.last_error is not None:
                return "error", 503
            if (
                self.last_cycle_started_at is not None
                and stale_after_secs > 0
                and now - self.last_cycle_started_at > stale_after_secs
            ):
                return "stalled", 503
            return "starting", 200

        if (
            stale_after_secs > 0
            and now - self.last_successful_cycle_at > stale_after_secs
        ):
            return "stale", 503
        if self.last_error is not None:
            return "degraded", 200
        return "ok", 200


def build_arg_parser() -> argparse.ArgumentParser:
    """Function docstring."""
    parser = argparse.ArgumentParser(
        description="Persistent autonomous Jupiter trading loop"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute real swaps instead of dry-run quotes",
    )
    parser.add_argument(
        "--entry-amount-sol",
        type=float,
        default=0.25,
        help="Target SOL amount per new position before risk sizing",
    )
    parser.add_argument(
        "--enter-on",
        choices=["down", "up", "all"],
        default="down",
        help="Which alert direction should trigger entries",
    )
    parser.add_argument(
        "--max-open-positions",
        type=int,
        default=None,
        help="Optional cap on simultaneous open positions",
    )
    parser.add_argument(
        "--interval-secs",
        type=int,
        default=SCAN_INTERVAL_SECS,
        help="Loop sleep interval between cycles",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Optional finite number of cycles for testing",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DATA_DIR / "state.json",
        help="Path to the persistent state JSON file",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Function docstring."""
    args = build_arg_parser().parse_args(argv)
    trader = AutoTrader(
        dry_run=not args.live,
        entry_amount_sol=args.entry_amount_sol,
        enter_on=args.enter_on,
        max_open_positions=args.max_open_positions,
        scan_interval_secs=args.interval_secs,
        state_path=args.state_file,
    )

    def handle_signal(sig: int, frame: Optional[FrameType]) -> None:
        """Function docstring."""
        del sig, frame
        trader.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    trader.run(max_iterations=args.iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
