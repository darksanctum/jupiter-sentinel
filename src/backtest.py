"""
Jupiter Sentinel - Historical Backtester
Replays scanner and risk manager logic on historical price series.
"""
import argparse
import csv
import json
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .analytics import TradingAnalytics
from .config import (
    MAX_POSITION_USD,
    PRICE_HISTORY_LEN,
    SCAN_PAIRS,
    STOP_LOSS_BPS,
    TAKE_PROFIT_BPS,
    VOLATILITY_THRESHOLD,
)
from .oracle import PricePoint
from .risk import Position, RiskManager
from .scanner import VolatilityScanner


@dataclass(frozen=True)
class HistoricalPriceRow:
    """One synchronized timestamp across one or more tracked pairs."""

    timestamp: datetime
    prices: Dict[str, float]


@dataclass
class BacktestResult:
    """Structured output from a historical replay."""

    source: str
    bars: int
    alerts: List[dict]
    trades: List[dict]
    equity_curve: List[dict]
    summary: Dict[str, float]


def _parse_timestamp(value: object) -> datetime:
    """Accept ISO-8601 strings, epoch numbers, or datetime objects."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(float(value))
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    raise TypeError(f"Unsupported timestamp value: {value!r}")


def _coerce_price_rows(records: Iterable[dict]) -> List[HistoricalPriceRow]:
    rows: List[HistoricalPriceRow] = []

    for raw in records:
        if "timestamp" not in raw:
            raise ValueError("Historical rows must include a 'timestamp' field.")

        prices = {}
        for key, value in raw.items():
            if key == "timestamp":
                continue
            if value in ("", None):
                continue
            prices[key] = float(value)

        rows.append(HistoricalPriceRow(timestamp=_parse_timestamp(raw["timestamp"]), prices=prices))

    rows.sort(key=lambda row: row.timestamp)
    if not rows:
        raise ValueError("No historical rows were loaded.")
    if "SOL/USDC" not in rows[0].prices:
        raise ValueError("Historical data must include a 'SOL/USDC' column for wallet valuation.")

    return rows


def load_price_rows(path: Optional[Path]) -> Tuple[List[HistoricalPriceRow], str]:
    """
    Load historical price data from CSV or JSON.

    Supported wide-format schemas:
    - CSV:  timestamp,SOL/USDC,JUP/USDC,...
    - JSON: [{ "timestamp": "...", "SOL/USDC": 123.4, ... }, ...]
    """
    if path is None:
        return generate_sample_rows(), "synthetic sample"

    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        with path.open(newline="") as handle:
            return _coerce_price_rows(csv.DictReader(handle)), str(path)

    if suffix == ".json":
        with path.open() as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError("JSON backtest input must be a list of row objects.")
        return _coerce_price_rows(payload), str(path)

    raise ValueError(f"Unsupported data format: {path.suffix or '<no suffix>'}")


def generate_sample_rows(steps: int = 72, interval_minutes: int = 30) -> List[HistoricalPriceRow]:
    """Build a deterministic sample series that produces scanner alerts and exits."""
    start = datetime(2026, 4, 10, 0, 0, 0)
    rows: List[HistoricalPriceRow] = []

    for index in range(steps):
        timestamp = start + timedelta(minutes=index * interval_minutes)
        sol = 100.0 + math.sin(index / 6.0) * 2.5
        bonk = 0.00002 * (1.0 + math.sin(index / 9.0) * 0.05)
        wif = 1.8 * (1.0 + math.sin(index / 8.0) * 0.03)
        jup_sol = 0.0105 * (1.0 + math.sin(index / 7.0) * 0.02)

        jup_usdc = 1.0 + math.sin(index / 5.0) * 0.01
        if 10 <= index <= 14:
            jup_usdc *= 1.0 - 0.025 * (index - 9)
        elif 15 <= index <= 20:
            jup_usdc *= 0.875 + 0.05 * (index - 14)
        elif 32 <= index <= 36:
            jup_usdc *= 1.0 - 0.03 * (index - 31)
        elif 37 <= index <= 42:
            jup_usdc *= 0.85 + 0.055 * (index - 36)

        rows.append(
            HistoricalPriceRow(
                timestamp=timestamp,
                prices={
                    "SOL/USDC": sol,
                    "JUP/USDC": jup_usdc,
                    "JUP/SOL": jup_sol,
                    "BONK/USDC": bonk,
                    "WIF/USDC": wif,
                },
            )
        )

    return rows


def available_pairs(rows: Sequence[HistoricalPriceRow]) -> List[Tuple[str, str, str]]:
    """Return configured scan pairs that are fully present in the dataset."""
    if not rows:
        return []

    return [pair for pair in SCAN_PAIRS if all(pair[2] in row.prices for row in rows)]


class HistoricalPriceFeed:
    """A `PriceFeed`-compatible adapter over a fixed historical series."""

    def __init__(
        self,
        pair_name: str,
        input_mint: str,
        output_mint: str,
        points: Sequence[PricePoint],
    ) -> None:
        self.pair_name = pair_name
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.points = list(points)
        self.history = deque(maxlen=PRICE_HISTORY_LEN)
        self._index = -1
        self._recorded_index = -1

    def set_index(self, index: int) -> None:
        self._index = index

    def fetch_price(self) -> Optional[PricePoint]:
        if self._index < 0 or self._index >= len(self.points):
            return None

        point = self.points[self._index]
        if self._recorded_index != self._index:
            self.history.append(point)
            self._recorded_index = self._index
        return point

    @property
    def current_price(self) -> Optional[float]:
        if self.history:
            return self.history[-1].price
        if 0 <= self._index < len(self.points):
            return self.points[self._index].price
        return None

    @property
    def volatility(self) -> float:
        if len(self.history) < 3:
            return 0.0

        prices = [point.price for point in self.history]
        returns = [(prices[idx] - prices[idx - 1]) / prices[idx - 1] for idx in range(1, len(prices))]
        if not returns:
            return 0.0

        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        return variance ** 0.5

    @property
    def price_change_pct(self) -> float:
        if len(self.history) < 2:
            return 0.0
        first = self.history[0].price
        last = self.history[-1].price
        if first == 0:
            return 0.0
        return (last - first) / first

    def stats(self) -> dict:
        return {
            "pair": self.pair_name,
            "price": self.current_price,
            "volatility": self.volatility,
            "change_pct": self.price_change_pct,
            "data_points": len(self.history),
        }


class HistoricalScanner(VolatilityScanner):
    """Scanner variant that timestamps alerts with historical bar times."""

    def __init__(self, feeds: Sequence[HistoricalPriceFeed]) -> None:
        self.feeds = list(feeds)
        self.alerts: List[dict] = []
        self.running = False

    def scan_once(self) -> List[dict]:
        new_alerts = []

        for feed in self.feeds:
            point = feed.fetch_price()
            if not point:
                continue

            if abs(feed.price_change_pct) > VOLATILITY_THRESHOLD and len(feed.history) >= 5:
                alert = {
                    "timestamp": datetime.utcfromtimestamp(point.timestamp).isoformat(),
                    "pair": feed.pair_name,
                    "price": point.price,
                    "change_pct": feed.price_change_pct * 100,
                    "volatility": feed.volatility,
                    "direction": "UP" if feed.price_change_pct > 0 else "DOWN",
                    "severity": "HIGH" if abs(feed.price_change_pct) > 0.10 else "MEDIUM",
                }
                new_alerts.append(alert)
                self.alerts.append(alert)

        return new_alerts


class HistoricalExecutor:
    """A small account simulator that mirrors the `TradeExecutor` balance API."""

    def __init__(self, starting_sol: float, starting_sol_price: float) -> None:
        self.address = "BACKTEST"
        self.cash_sol = float(starting_sol)
        self.sol_price = float(starting_sol_price)
        self.trade_history: List[dict] = []

    def set_sol_price(self, price: float) -> None:
        self.sol_price = float(price)

    def reserve_position(self, amount_sol: float, pair: str, timestamp: float) -> None:
        self.cash_sol = max(0.0, self.cash_sol - amount_sol)
        self.trade_history.append(
            {
                "timestamp": datetime.utcfromtimestamp(timestamp).isoformat(),
                "pair": pair,
                "type": "OPEN",
                "amount_sol": amount_sol,
                "sol_price": self.sol_price,
                "status": "success",
            }
        )

    def settle_position(
        self,
        position: Position,
        pnl_pct: float,
        exit_price: float,
        action_type: str,
        timestamp: float,
    ) -> None:
        proceeds_sol = max(position.amount_sol * (1.0 + (pnl_pct / 100.0)), 0.0)
        self.cash_sol += proceeds_sol
        self.trade_history.append(
            {
                "timestamp": datetime.utcfromtimestamp(timestamp).isoformat(),
                "pair": position.pair,
                "type": action_type,
                "amount_sol": position.amount_sol,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "status": "success",
            }
        )

    def get_balance(self) -> dict:
        return {
            "sol": self.cash_sol,
            "usd_value": self.cash_sol * self.sol_price,
            "sol_price": self.sol_price,
            "address": self.address,
        }


class HistoricalRiskManager(RiskManager):
    """Risk manager that timestamps and settles positions against historical bars."""

    def __init__(self, executor: HistoricalExecutor, feeds_by_pair: Dict[str, HistoricalPriceFeed]) -> None:
        super().__init__(executor)
        self._feeds_by_pair = feeds_by_pair

    def open_position(
        self,
        pair: str,
        input_mint: str,
        output_mint: str,
        amount_sol: float,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        dry_run: bool = True,
    ) -> Optional[Position]:
        balance = self.executor.get_balance()
        sol_price = float(balance.get("sol_price", 0.0) or 0.0)
        if sol_price <= 0:
            return None

        max_sol = min(
            amount_sol,
            MAX_POSITION_USD / sol_price,
            balance["sol"] * 0.8,
        )
        if max_sol < 0.001:
            return None

        feed = self._feeds_by_pair.get(pair)
        point = feed.fetch_price() if feed else None
        if not point:
            return None

        position = Position(
            pair=pair,
            input_mint=input_mint,
            output_mint=output_mint,
            entry_price=point.price,
            amount_sol=max_sol,
            entry_time=point.timestamp,
            stop_loss_pct=stop_loss_pct or (STOP_LOSS_BPS / 10000),
            take_profit_pct=take_profit_pct or (TAKE_PROFIT_BPS / 10000),
            highest_price=point.price,
        )

        position.notional = max_sol * sol_price
        self.positions.append(position)
        self.price_feeds[pair] = feed
        self.executor.reserve_position(max_sol, pair, point.timestamp)
        return position

    def check_positions(self) -> List[dict]:
        actions = []

        for pos in self.positions[:]:
            if pos.status != "open":
                continue

            feed = self.price_feeds.get(pos.pair)
            point = feed.fetch_price() if feed else None
            if not point:
                continue

            current_price = point.price
            pnl_decimal = (current_price - pos.entry_price) / pos.entry_price

            if current_price > pos.highest_price:
                pos.highest_price = current_price

            trailing_stop_price = pos.highest_price * (1.0 - pos.trailing_stop_pct)
            action = None

            if pnl_decimal <= -pos.stop_loss_pct:
                action = {
                    "type": "STOP_LOSS",
                    "pair": pos.pair,
                    "pnl_pct": pnl_decimal * 100,
                    "price": current_price,
                }
            elif pnl_decimal >= pos.take_profit_pct:
                action = {
                    "type": "TAKE_PROFIT",
                    "pair": pos.pair,
                    "pnl_pct": pnl_decimal * 100,
                    "price": current_price,
                }
            elif current_price <= trailing_stop_price and pos.highest_price > pos.entry_price * 1.01:
                action = {
                    "type": "TRAILING_STOP",
                    "pair": pos.pair,
                    "pnl_pct": pnl_decimal * 100,
                    "price": current_price,
                    "highest": pos.highest_price,
                }

            if not action:
                continue

            notional = float(getattr(pos, "notional", pos.amount_sol * self.executor.sol_price))
            pnl_amount = notional * pnl_decimal
            pos.status = "closed"
            self.positions.remove(pos)
            self.executor.settle_position(pos, action["pnl_pct"], current_price, action["type"], point.timestamp)
            self.closed_positions.append(
                {
                    "position": pos,
                    "action": action,
                    "timestamp": datetime.utcfromtimestamp(point.timestamp).isoformat(),
                    "notional": notional,
                    "pnl_amount": pnl_amount,
                }
            )
            actions.append(action)

        return actions


class HistoricalBacktester:
    """Replay the scanner and risk manager over historical bars."""

    def __init__(
        self,
        rows: Sequence[HistoricalPriceRow],
        *,
        starting_sol: float = 10.0,
        entry_amount_sol: float = 0.25,
        enter_on: str = "down",
    ) -> None:
        if not rows:
            raise ValueError("Backtest requires at least one price row.")
        if enter_on not in {"down", "up", "all"}:
            raise ValueError("enter_on must be one of: down, up, all")

        self.rows = list(rows)
        self.enter_on = enter_on
        self.entry_amount_sol = float(entry_amount_sol)
        self.pairs = available_pairs(self.rows)
        if not self.pairs:
            raise ValueError("Historical data does not include any configured scan pairs.")
        if "SOL/USDC" not in {pair_name for _, _, pair_name in self.pairs}:
            raise ValueError("Backtest requires the SOL/USDC pair.")

        self.feeds_by_pair = self._build_feeds()
        self.scanner = HistoricalScanner(list(self.feeds_by_pair.values()))
        self.executor = HistoricalExecutor(starting_sol=starting_sol, starting_sol_price=self.rows[0].prices["SOL/USDC"])
        self.risk_manager = HistoricalRiskManager(self.executor, self.feeds_by_pair)
        self.analytics = TradingAnalytics(starting_equity=starting_sol * self.rows[0].prices["SOL/USDC"])
        self.equity_snapshots: List[dict] = []

    def _build_feeds(self) -> Dict[str, HistoricalPriceFeed]:
        feeds = {}
        for input_mint, output_mint, pair_name in self.pairs:
            points = [
                PricePoint(timestamp=row.timestamp.timestamp(), price=row.prices[pair_name])
                for row in self.rows
            ]
            feeds[pair_name] = HistoricalPriceFeed(
                pair_name=pair_name,
                input_mint=input_mint,
                output_mint=output_mint,
                points=points,
            )
        return feeds

    def _has_open_position(self, pair_name: str) -> bool:
        return any(position.pair == pair_name and position.status == "open" for position in self.risk_manager.positions)

    def _should_open(self, alert: dict) -> bool:
        direction = alert["direction"].lower()
        return self.enter_on == "all" or direction == self.enter_on

    def _record_equity_snapshot(self, timestamp: datetime) -> None:
        total_sol = self.executor.cash_sol

        for pos in self.risk_manager.positions:
            feed = self.risk_manager.price_feeds.get(pos.pair)
            current_price = feed.current_price if feed else pos.entry_price
            pnl_decimal = ((current_price or pos.entry_price) - pos.entry_price) / pos.entry_price
            total_sol += max(pos.amount_sol * (1.0 + pnl_decimal), 0.0)

        self.equity_snapshots.append(
            {
                "timestamp": timestamp.isoformat(),
                "equity": total_sol * self.executor.sol_price,
            }
        )

    def _summary(self) -> Dict[str, float]:
        if not self.equity_snapshots:
            return {}

        start_equity = self.equity_snapshots[0]["equity"]
        end_equity = self.equity_snapshots[-1]["equity"]
        peak = start_equity
        max_drawdown = 0.0

        for point in self.equity_snapshots:
            peak = max(peak, point["equity"])
            if peak == 0:
                continue
            drawdown = (peak - point["equity"]) / peak
            max_drawdown = max(max_drawdown, drawdown)

        realized_pnl = sum(
            trade.pnl_amount for trade in self.analytics.realized_trades if trade.pnl_amount is not None
        )
        return {
            "starting_equity": start_equity,
            "ending_equity": end_equity,
            "total_return_pct": ((end_equity - start_equity) / start_equity * 100.0) if start_equity else 0.0,
            "realized_pnl": realized_pnl,
            "max_drawdown_pct": max_drawdown * 100.0,
            "alerts": float(len(self.scanner.alerts)),
            "closed_trades": float(len(self.analytics.realized_trades)),
            "win_rate": self.analytics.calculate_win_rate(),
            "open_positions": float(len(self.risk_manager.positions)),
        }

    def _trade_rows(self) -> List[dict]:
        rows = []
        for trade in self.analytics.realized_trades:
            rows.append(
                {
                    "pair": trade.pair,
                    "opened_at": trade.opened_at.isoformat(),
                    "closed_at": trade.closed_at.isoformat(),
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "pnl_pct": trade.pnl_pct,
                    "pnl_amount": trade.pnl_amount,
                    "action_type": trade.metadata.get("action_type", ""),
                }
            )
        return rows

    def run(self) -> BacktestResult:
        closed_offset = 0

        for index, row in enumerate(self.rows):
            self.executor.set_sol_price(row.prices["SOL/USDC"])
            for feed in self.feeds_by_pair.values():
                feed.set_index(index)

            alerts = self.scanner.scan_once()
            self.risk_manager.check_positions()

            for closed in self.risk_manager.closed_positions[closed_offset:]:
                self.analytics.record_closed_position(closed)
            closed_offset = len(self.risk_manager.closed_positions)

            for alert in alerts:
                if not self._should_open(alert):
                    continue
                if self._has_open_position(alert["pair"]):
                    continue

                pair_meta = next((pair for pair in self.pairs if pair[2] == alert["pair"]), None)
                if pair_meta is None:
                    continue

                self.risk_manager.open_position(
                    pair=pair_meta[2],
                    input_mint=pair_meta[0],
                    output_mint=pair_meta[1],
                    amount_sol=self.entry_amount_sol,
                    dry_run=True,
                )

            self._record_equity_snapshot(row.timestamp)

        return BacktestResult(
            source="historical replay",
            bars=len(self.rows),
            alerts=list(self.scanner.alerts),
            trades=self._trade_rows(),
            equity_curve=list(self.equity_snapshots),
            summary=self._summary(),
        )


def render_equity_curve(curve: Sequence[dict], width: int = 64, height: int = 12) -> str:
    """Render a compact ASCII equity curve."""
    if not curve:
        return "No equity data."

    if len(curve) == 1:
        point = curve[0]
        return f"{point['timestamp'][:19]}  ${point['equity']:.2f}"

    if len(curve) <= width:
        sampled = list(curve)
    else:
        sampled = [
            curve[round(index * (len(curve) - 1) / (width - 1))]
            for index in range(width)
        ]

    values = [point["equity"] for point in sampled]
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum

    grid = [[" " for _ in range(len(sampled))] for _ in range(height)]
    for column, value in enumerate(values):
        if span == 0:
            row = height // 2
        else:
            row = round((maximum - value) / span * (height - 1))
        grid[row][column] = "*"

    lines = [f"max ${maximum:.2f}"]
    lines.extend("".join(row) for row in grid)
    lines.append(f"min ${minimum:.2f}")
    lines.append(f"{sampled[0]['timestamp'][:19]} -> {sampled[-1]['timestamp'][:19]}")
    return "\n".join(lines)


def format_backtest_report(result: BacktestResult) -> str:
    """Human-friendly terminal output for CLI usage."""
    summary = result.summary
    lines = [
        "JUPITER SENTINEL BACKTEST",
        "=" * 48,
        f"Bars: {result.bars}",
        f"Alerts: {int(summary.get('alerts', 0))}",
        f"Closed trades: {int(summary.get('closed_trades', 0))}",
        f"Win rate: {summary.get('win_rate', 0.0):.2f}%",
        f"Starting equity: ${summary.get('starting_equity', 0.0):.2f}",
        f"Ending equity: ${summary.get('ending_equity', 0.0):.2f}",
        f"Total return: {summary.get('total_return_pct', 0.0):+.2f}%",
        f"Realized P&L: ${summary.get('realized_pnl', 0.0):+.2f}",
        f"Max drawdown: {summary.get('max_drawdown_pct', 0.0):.2f}%",
        "",
        "Equity Curve",
        "-" * 48,
        render_equity_curve(result.equity_curve),
    ]

    if result.trades:
        lines.extend(["", "Recent Trades", "-" * 48])
        for trade in result.trades[-5:]:
            lines.append(
                "{closed_at}  {pair:10s}  {action_type:13s}  {pnl_pct:+6.2f}%  {pnl_amount:+7.2f}".format(
                    closed_at=trade["closed_at"][:19],
                    pair=trade["pair"],
                    action_type=trade["action_type"] or "EXIT",
                    pnl_pct=trade["pnl_pct"],
                    pnl_amount=trade["pnl_amount"] or 0.0,
                )
            )

    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Jupiter Sentinel logic on historical price data.")
    parser.add_argument("--data", type=Path, help="Path to CSV or JSON price history. Uses a synthetic sample when omitted.")
    parser.add_argument("--starting-sol", type=float, default=10.0, help="Starting SOL balance for the simulated wallet.")
    parser.add_argument("--entry-amount-sol", type=float, default=0.25, help="Requested SOL per entry before risk caps.")
    parser.add_argument(
        "--enter-on",
        choices=("down", "up", "all"),
        default="down",
        help="Scanner alert direction that opens a new position.",
    )
    args = parser.parse_args(argv)

    rows, source = load_price_rows(args.data)
    result = HistoricalBacktester(
        rows,
        starting_sol=args.starting_sol,
        entry_amount_sol=args.entry_amount_sol,
        enter_on=args.enter_on,
    ).run()
    print(f"Data source: {source}")
    print()
    print(format_backtest_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
