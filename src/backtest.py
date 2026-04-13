"""
Jupiter Sentinel - Historical Backtester
Replays scanner and risk manager logic on historical price series.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .analytics import TradingAnalytics
from .config import (
    DATA_DIR,
    MAX_POSITION_USD,
    PRICE_HISTORY_LEN,
    SCAN_PAIRS,
    STOP_LOSS_BPS,
    TAKE_PROFIT_BPS,
    VOLATILITY_THRESHOLD,
)
from .oracle import PricePoint
from .resilience import atomic_write_text
from .risk import Position, RiskManager
from .scanner import VolatilityScanner
from .strategies.mean_reversion import scan_for_signals as scan_mean_reversion_signals
from .strategies.momentum import scan_for_signals as scan_momentum_signals


@dataclass(frozen=True)
class HistoricalPriceRow:
    """One synchronized timestamp across one or more tracked pairs."""

    timestamp: datetime
    prices: Dict[str, float]


@dataclass
class BacktestResult:
    """Structured output from a historical replay."""

    strategy: str
    description: str
    source: str
    bars: int
    alerts: List[dict]
    trades: List[dict]
    equity_curve: List[dict]
    summary: Dict[str, float]


@dataclass(frozen=True)
class BacktestStrategy:
    """Strategy configuration for the historical replay engine."""

    name: str
    description: str
    signal_generator: Callable[[Sequence[HistoricalPriceFeed], HistoricalScanner], List[dict]]
    should_open: Callable[[dict], bool]


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


TIMESTAMP_FIELDS = ("timestamp", "time", "datetime", "date")
KNOWN_PAIR_NAMES = tuple(pair_name for _, _, pair_name in SCAN_PAIRS)


def _extract_timestamp(raw: dict) -> object:
    for key in TIMESTAMP_FIELDS:
        if key in raw and raw[key] not in ("", None):
            return raw[key]
    raise ValueError("Historical rows must include a timestamp-like field.")


def _normalize_pair_name(value: str) -> str:
    candidate = re.sub(r"\s+", "", value.strip().upper())
    candidate = candidate.replace("-", "/").replace("_", "/")
    return candidate


def _infer_pair_name(path: Path) -> Optional[str]:
    stem = path.stem.upper()
    normalized = _normalize_pair_name(stem)
    if normalized in KNOWN_PAIR_NAMES:
        return normalized

    tokens = [token for token in re.split(r"[^A-Z0-9]+", stem) if token]
    for index in range(len(tokens) - 1):
        candidate = f"{tokens[index]}/{tokens[index + 1]}"
        if candidate in KNOWN_PAIR_NAMES:
            return candidate
    return None


def _extract_json_rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("rows", "data", "prices"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested

    raise ValueError("JSON backtest input must be a list of row objects or contain a list under rows/data/prices.")


def _finalize_price_rows(rows: Iterable[HistoricalPriceRow]) -> List[HistoricalPriceRow]:
    ordered_rows = sorted(rows, key=lambda row: row.timestamp)
    if not ordered_rows:
        raise ValueError("No historical rows were loaded.")

    synchronized_rows: List[HistoricalPriceRow] = []
    last_prices: Dict[str, float] = {}

    for row in ordered_rows:
        merged_prices = dict(last_prices)
        merged_prices.update(row.prices)
        if not merged_prices:
            continue
        last_prices = merged_prices
        synchronized_rows.append(HistoricalPriceRow(timestamp=row.timestamp, prices=merged_prices))

    if not synchronized_rows:
        raise ValueError("No valid price points were loaded.")

    start_index = None
    for index, row in enumerate(synchronized_rows):
        available = {pair for pair in KNOWN_PAIR_NAMES if pair in row.prices}
        if "SOL/USDC" in available and len(available) >= 2:
            start_index = index
            break

    if start_index is None:
        raise ValueError("Historical data must include SOL/USDC and at least one tradable pair.")

    synchronized_rows = synchronized_rows[start_index:]
    common_pairs = {pair for pair in KNOWN_PAIR_NAMES if pair in synchronized_rows[0].prices}
    for row in synchronized_rows[1:]:
        common_pairs.intersection_update(row.prices)

    ordered_pairs = [pair for pair in KNOWN_PAIR_NAMES if pair in common_pairs]
    if "SOL/USDC" not in ordered_pairs:
        raise ValueError("Historical data must include a 'SOL/USDC' column for wallet valuation.")
    if len(ordered_pairs) < 2:
        raise ValueError("Historical data must include SOL/USDC and at least one strategy pair.")

    return [
        HistoricalPriceRow(
            timestamp=row.timestamp,
            prices={pair_name: row.prices[pair_name] for pair_name in ordered_pairs},
        )
        for row in synchronized_rows
    ]


def _coerce_price_rows(records: Iterable[dict]) -> List[HistoricalPriceRow]:
    rows: List[HistoricalPriceRow] = []

    for raw in records:
        timestamp = _extract_timestamp(raw)

        prices = {}
        for key, value in raw.items():
            if key in TIMESTAMP_FIELDS:
                continue
            if value in ("", None):
                continue
            normalized_key = _normalize_pair_name(str(key))
            prices[normalized_key] = float(value)

        rows.append(HistoricalPriceRow(timestamp=_parse_timestamp(timestamp), prices=prices))

    return _finalize_price_rows(rows)


def _load_records_from_path(path: Path) -> List[dict]:
    suffix = path.suffix.lower()

    if suffix == ".csv":
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle))

    if suffix == ".json":
        with path.open() as handle:
            return _extract_json_rows(json.load(handle))

    raise ValueError(f"Unsupported data format: {path.suffix or '<no suffix>'}")


def _merge_directory_records(rows_by_timestamp: Dict[datetime, Dict[str, float]], path: Path) -> None:
    pair_hint = _infer_pair_name(path)

    for raw in _load_records_from_path(path):
        if not isinstance(raw, dict):
            raise ValueError(f"Historical records in {path} must be objects.")

        timestamp = _parse_timestamp(_extract_timestamp(raw))
        bucket = rows_by_timestamp.setdefault(timestamp, {})

        pair_value = raw.get("pair") or raw.get("symbol") or raw.get("market")
        if pair_value not in ("", None) and raw.get("price") not in ("", None):
            bucket[_normalize_pair_name(str(pair_value))] = float(raw["price"])
            continue

        if pair_hint and raw.get("price") not in ("", None):
            bucket[pair_hint] = float(raw["price"])
            continue

        for key, value in raw.items():
            if key in TIMESTAMP_FIELDS or value in ("", None):
                continue
            normalized_key = _normalize_pair_name(str(key))
            if normalized_key in KNOWN_PAIR_NAMES or "/" in normalized_key:
                bucket[normalized_key] = float(value)


def load_price_rows_from_directory(directory: Path) -> Tuple[List[HistoricalPriceRow], str]:
    """Load and synchronize all supported historical price files inside a directory."""
    files = sorted(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in {".csv", ".json"})
    if not files:
        raise ValueError(f"No historical price files were found in {directory}.")

    rows_by_timestamp: Dict[datetime, Dict[str, float]] = {}
    for path in files:
        _merge_directory_records(rows_by_timestamp, path)

    rows = _finalize_price_rows(
        HistoricalPriceRow(timestamp=timestamp, prices=prices)
        for timestamp, prices in rows_by_timestamp.items()
    )
    return rows, f"{directory} ({len(files)} files)"


def load_price_rows(path: Optional[Path]) -> Tuple[List[HistoricalPriceRow], str]:
    """
    Load historical price data from a file or directory.

    Supported wide-format schemas:
    - CSV:  timestamp,SOL/USDC,JUP/USDC,...
    - JSON: [{ "timestamp": "...", "SOL/USDC": 123.4, ... }, ...]
    Supported directory schemas:
    - wide files merged by timestamp
    - long files with timestamp,pair,price
    - pair-specific files with timestamp,price (pair inferred from filename)
    """
    if path is None:
        path = DATA_DIR

    path = Path(path)
    if path.is_dir():
        files = [
            file_path
            for file_path in path.rglob("*")
            if file_path.is_file() and file_path.suffix.lower() in {".csv", ".json"}
        ]
        if not files:
            return generate_sample_rows(), "synthetic sample"
        return load_price_rows_from_directory(path)

    return _coerce_price_rows(_load_records_from_path(path)), str(path)


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


def _volatility_signal_generator(feeds: Sequence[HistoricalPriceFeed], scanner: HistoricalScanner) -> List[dict]:
    del feeds
    return scanner.scan_once()


def _momentum_signal_generator(feeds: Sequence[HistoricalPriceFeed], scanner: HistoricalScanner) -> List[dict]:
    del scanner
    return scan_momentum_signals(feeds)


def _mean_reversion_signal_generator(feeds: Sequence[HistoricalPriceFeed], scanner: HistoricalScanner) -> List[dict]:
    del scanner
    return scan_mean_reversion_signals(feeds)


def build_volatility_strategy(enter_on: str = "down") -> BacktestStrategy:
    if enter_on not in {"down", "up", "all"}:
        raise ValueError("enter_on must be one of: down, up, all")

    return BacktestStrategy(
        name="volatility_reversal",
        description=f"Volatility scanner entries on {enter_on.upper()} alerts with risk-managed exits.",
        signal_generator=_volatility_signal_generator,
        should_open=lambda alert: enter_on == "all" or str(alert.get("direction", "")).lower() == enter_on,
    )


DEFAULT_BACKTEST_STRATEGIES: Tuple[BacktestStrategy, ...] = (
    build_volatility_strategy("down"),
    BacktestStrategy(
        name="momentum",
        description="Long-only momentum entries on consecutive upward price moves.",
        signal_generator=_momentum_signal_generator,
        should_open=lambda alert: alert.get("action") == "BUY" and alert.get("side") == "LONG",
    ),
    BacktestStrategy(
        name="mean_reversion",
        description="Long-only Bollinger mean-reversion entries when price falls below the lower band.",
        signal_generator=_mean_reversion_signal_generator,
        should_open=lambda alert: alert.get("action") == "BUY" and alert.get("side") == "LONG",
    ),
)


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
        strategy: Optional[BacktestStrategy] = None,
    ) -> None:
        if not rows:
            raise ValueError("Backtest requires at least one price row.")

        self.rows = list(rows)
        self.strategy = strategy or build_volatility_strategy(enter_on)
        self.entry_amount_sol = float(entry_amount_sol)
        self.pairs = available_pairs(self.rows)
        if not self.pairs:
            raise ValueError("Historical data does not include any configured scan pairs.")
        self.tradable_pairs = [pair for pair in self.pairs if pair[2] != "SOL/USDC"]
        self.tradable_pair_names = {pair_name for _, _, pair_name in self.tradable_pairs}
        if "SOL/USDC" not in {pair_name for _, _, pair_name in self.pairs}:
            raise ValueError("Backtest requires the SOL/USDC pair.")
        if not self.tradable_pairs:
            raise ValueError("Backtest requires at least one tradable pair besides SOL/USDC.")

        self.feeds_by_pair = self._build_feeds()
        self.scanner = HistoricalScanner(list(self.feeds_by_pair.values()))
        self.executor = HistoricalExecutor(starting_sol=starting_sol, starting_sol_price=self.rows[0].prices["SOL/USDC"])
        self.risk_manager = HistoricalRiskManager(self.executor, self.feeds_by_pair)
        self.analytics = TradingAnalytics(starting_equity=starting_sol * self.rows[0].prices["SOL/USDC"])
        self.equity_snapshots: List[dict] = []
        self.alerts: List[dict] = []

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
        return self.strategy.should_open(alert)

    def _period_returns(self) -> List[float]:
        returns: List[float] = []
        previous_equity = None

        for point in self.equity_snapshots:
            equity = float(point["equity"])
            if previous_equity and previous_equity > 0:
                returns.append((equity - previous_equity) / previous_equity)
            previous_equity = equity

        return returns

    def _periods_per_year(self) -> float:
        if len(self.equity_snapshots) < 2:
            return 365.0

        deltas = []
        previous = _parse_timestamp(self.equity_snapshots[0]["timestamp"])
        for point in self.equity_snapshots[1:]:
            current = _parse_timestamp(point["timestamp"])
            delta_seconds = (current - previous).total_seconds()
            if delta_seconds > 0:
                deltas.append(delta_seconds)
            previous = current

        if not deltas:
            return 365.0

        return max(1.0, (365.25 * 24 * 60 * 60) / median(deltas))

    def _calculate_sharpe_ratio(self) -> float:
        returns = self._period_returns()
        if len(returns) < 2:
            return 0.0

        mean_return = sum(returns) / len(returns)
        variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
        volatility = variance ** 0.5
        if volatility == 0:
            return 0.0

        return mean_return / volatility * math.sqrt(self._periods_per_year())

    def _calculate_sortino_ratio(self) -> float:
        returns = self._period_returns()
        if not returns:
            return 0.0

        mean_return = sum(returns) / len(returns)
        downside_variance = sum(min(value, 0.0) ** 2 for value in returns) / len(returns)
        downside_deviation = downside_variance ** 0.5
        if downside_deviation == 0:
            return 0.0

        return mean_return / downside_deviation * math.sqrt(self._periods_per_year())

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
            "alerts": len(self.alerts),
            "closed_trades": len(self.analytics.realized_trades),
            "win_rate": self.analytics.calculate_win_rate(),
            "open_positions": len(self.risk_manager.positions),
            "sharpe_ratio": self._calculate_sharpe_ratio(),
            "sortino_ratio": self._calculate_sortino_ratio(),
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
                feed.fetch_price()

            alerts = [
                {
                    **alert,
                    "strategy": alert.get("strategy", self.strategy.name),
                }
                for alert in self.strategy.signal_generator(list(self.feeds_by_pair.values()), self.scanner)
                if alert.get("pair") in self.tradable_pair_names
            ]
            self.alerts.extend(alerts)
            self.risk_manager.check_positions()

            for closed in self.risk_manager.closed_positions[closed_offset:]:
                self.analytics.record_closed_position(closed)
            closed_offset = len(self.risk_manager.closed_positions)

            for alert in alerts:
                if not self._should_open(alert):
                    continue
                if self._has_open_position(alert["pair"]):
                    continue

                pair_meta = next((pair for pair in self.tradable_pairs if pair[2] == alert["pair"]), None)
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
            strategy=self.strategy.name,
            description=self.strategy.description,
            source="historical replay",
            bars=len(self.rows),
            alerts=list(self.alerts),
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
        f"JUPITER SENTINEL BACKTEST [{result.strategy}]",
        "=" * 48,
        result.description,
        "",
        f"Bars: {result.bars}",
        f"Alerts: {int(summary.get('alerts', 0))}",
        f"Closed trades: {int(summary.get('closed_trades', 0))}",
        f"Win rate: {summary.get('win_rate', 0.0):.2f}%",
        f"Sharpe ratio: {summary.get('sharpe_ratio', 0.0):.2f}",
        f"Sortino ratio: {summary.get('sortino_ratio', 0.0):.2f}",
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


def run_parallel_backtests(
    rows: Sequence[HistoricalPriceRow],
    *,
    strategies: Optional[Sequence[BacktestStrategy]] = None,
    starting_sol: float = 10.0,
    entry_amount_sol: float = 0.25,
) -> List[BacktestResult]:
    """Run multiple backtest strategies concurrently and preserve the requested order."""
    strategy_list = list(strategies or DEFAULT_BACKTEST_STRATEGIES)
    if not strategy_list:
        raise ValueError("At least one strategy is required.")

    results: List[Optional[BacktestResult]] = [None] * len(strategy_list)
    max_workers = max(1, min(len(strategy_list), 8))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                HistoricalBacktester(
                    rows,
                    starting_sol=starting_sol,
                    entry_amount_sol=entry_amount_sol,
                    strategy=strategy,
                ).run
            ): index
            for index, strategy in enumerate(strategy_list)
        }

        for future in as_completed(future_map):
            results[future_map[future]] = future.result()

    return [result for result in results if result is not None]


def format_strategy_comparison_report(results: Sequence[BacktestResult], *, source: str) -> str:
    """Build a markdown comparison report with per-strategy metrics and equity curves."""
    ordered_results = sorted(results, key=lambda result: result.summary.get("ending_equity", 0.0), reverse=True)
    generated_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    lines = [
        "# Jupiter Sentinel Backtest Report",
        "",
        f"- Generated: {generated_at}",
        f"- Data source: {source}",
        f"- Strategies tested: {len(ordered_results)}",
        "",
        "## Comparison",
        "",
        "| Strategy | Alerts | Closed Trades | Win Rate | Sharpe | Sortino | Max Drawdown | Total Return | Ending Equity |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for result in ordered_results:
        summary = result.summary
        lines.append(
            "| {strategy} | {alerts} | {closed_trades} | {win_rate:.2f}% | {sharpe:.2f} | {sortino:.2f} | {drawdown:.2f}% | {total_return:+.2f}% | ${ending_equity:.2f} |".format(
                strategy=result.strategy,
                alerts=int(summary.get("alerts", 0)),
                closed_trades=int(summary.get("closed_trades", 0)),
                win_rate=summary.get("win_rate", 0.0),
                sharpe=summary.get("sharpe_ratio", 0.0),
                sortino=summary.get("sortino_ratio", 0.0),
                drawdown=summary.get("max_drawdown_pct", 0.0),
                total_return=summary.get("total_return_pct", 0.0),
                ending_equity=summary.get("ending_equity", 0.0),
            )
        )

    for result in ordered_results:
        summary = result.summary
        lines.extend(
            [
                "",
                f"## {result.strategy}",
                "",
                result.description,
                "",
                f"- Bars: {result.bars}",
                f"- Alerts: {int(summary.get('alerts', 0))}",
                f"- Closed trades: {int(summary.get('closed_trades', 0))}",
                f"- Win rate: {summary.get('win_rate', 0.0):.2f}%",
                f"- Sharpe ratio: {summary.get('sharpe_ratio', 0.0):.2f}",
                f"- Sortino ratio: {summary.get('sortino_ratio', 0.0):.2f}",
                f"- Max drawdown: {summary.get('max_drawdown_pct', 0.0):.2f}%",
                f"- Ending equity: ${summary.get('ending_equity', 0.0):.2f}",
                "",
                "### Equity Curve",
                "",
                "```text",
                render_equity_curve(result.equity_curve),
                "```",
            ]
        )

        if result.trades:
            lines.extend(
                [
                    "",
                    "### Recent Trades",
                    "",
                    "| Closed At | Pair | Action | PnL % | PnL |",
                    "| --- | --- | --- | ---: | ---: |",
                ]
            )
            for trade in result.trades[-10:]:
                lines.append(
                    "| {closed_at} | {pair} | {action} | {pnl_pct:+.2f}% | {pnl_amount:+.2f} |".format(
                        closed_at=trade["closed_at"][:19],
                        pair=trade["pair"],
                        action=trade["action_type"] or "EXIT",
                        pnl_pct=trade["pnl_pct"],
                        pnl_amount=trade["pnl_amount"] or 0.0,
                    )
                )

    return "\n".join(lines) + "\n"


def write_backtest_report(report: str, output_path: Path) -> Path:
    """Persist the markdown report to disk."""
    output_path = Path(output_path)
    return atomic_write_text(output_path, report, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Jupiter Sentinel logic on historical price data.")
    parser.add_argument(
        "--data",
        type=Path,
        default=DATA_DIR,
        help="Path to a CSV/JSON price file or a directory of historical price files.",
    )
    parser.add_argument("--starting-sol", type=float, default=10.0, help="Starting SOL balance for the simulated wallet.")
    parser.add_argument("--entry-amount-sol", type=float, default=0.25, help="Requested SOL per entry before risk caps.")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=DATA_DIR / "backtest_report.md",
        help="Markdown report output path.",
    )
    args = parser.parse_args(argv)

    rows, source = load_price_rows(args.data)
    results = run_parallel_backtests(
        rows,
        starting_sol=args.starting_sol,
        entry_amount_sol=args.entry_amount_sol,
    )
    report = format_strategy_comparison_report(results, source=source)
    report_path = write_backtest_report(report, args.report_path)

    print(f"Data source: {source}")
    print(f"Report written to: {report_path}")
    print()
    for result in results:
        print(format_backtest_report(result))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
