"""
Jupiter Sentinel - Trading Analytics
Tracks trade executions, realized performance metrics, and markdown reports.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import stdev
from typing import Any, Dict, List, Optional, Union


DateLike = Union[date, datetime, float, int, str]


@dataclass
class TradeExecution:
    """A normalized executor swap event."""

    timestamp: datetime
    input_mint: str
    output_mint: str
    amount: float
    status: str
    out_amount: float = 0.0
    out_usd: Optional[float] = None
    price_impact: float = 0.0
    tx_signature: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RealizedTrade:
    """A closed trade that can be used for performance analytics."""

    pair: str
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    pnl_pct: float
    notional: float = 0.0
    pnl_amount: Optional[float] = None
    side: str = "LONG"
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def return_decimal(self) -> float:
        """Function docstring."""
        return self.pnl_pct / 100.0

    @property
    def outcome(self) -> str:
        """Function docstring."""
        if self.pnl_pct > 0:
            return "win"
        if self.pnl_pct < 0:
            return "loss"
        return "flat"


def _coerce_datetime(value: Optional[DateLike]) -> datetime:
    """Function docstring."""
    if value is None:
        return datetime.utcnow()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(float(value))
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    raise TypeError(f"Unsupported timestamp value: {value!r}")


def _coerce_date(value: DateLike) -> date:
    """Function docstring."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return _coerce_datetime(value).date()


def _format_currency(value: Optional[float]) -> str:
    """Function docstring."""
    if value is None:
        return "n/a"
    if value < 0:
        return f"-${abs(value):.2f}"
    return f"${value:.2f}"


class TradingAnalytics:
    """
    Tracks normalized trade history and exposes portfolio metrics.

    The module keeps two separate ledgers:
    - `executions` for raw swap events coming from `TradeExecutor`
    - `realized_trades` for closed trades that can feed performance metrics
    """

    def __init__(
        self, starting_equity: float = 1.0, periods_per_year: int = 365
    ) -> None:
        """Function docstring."""
        self.starting_equity = float(starting_equity) if starting_equity > 0 else 1.0
        self.periods_per_year = periods_per_year
        self.executions: List[TradeExecution] = []
        self.realized_trades: List[RealizedTrade] = []

    def track_execution(self, trade_result: Dict[str, Any]) -> TradeExecution:
        """Normalize and store a raw swap event from `TradeExecutor`."""
        metadata = dict(trade_result)
        for key in (
            "timestamp",
            "input_mint",
            "output_mint",
            "amount",
            "status",
            "out_amount",
            "out_usd",
            "price_impact",
            "tx_signature",
        ):
            metadata.pop(key, None)

        execution = TradeExecution(
            timestamp=_coerce_datetime(trade_result.get("timestamp")),
            input_mint=trade_result.get("input_mint", ""),
            output_mint=trade_result.get("output_mint", ""),
            amount=float(trade_result.get("amount", 0.0) or 0.0),
            status=trade_result.get("status", "unknown"),
            out_amount=float(trade_result.get("out_amount", 0.0) or 0.0),
            out_usd=(
                float(trade_result["out_usd"])
                if trade_result.get("out_usd") is not None
                else None
            ),
            price_impact=float(trade_result.get("price_impact", 0.0) or 0.0),
            tx_signature=trade_result.get("tx_signature", ""),
            metadata=metadata,
        )
        self.executions.append(execution)
        return execution

    def record_trade(
        self,
        pair: str,
        pnl_pct: float,
        *,
        opened_at: Optional[DateLike] = None,
        closed_at: Optional[DateLike] = None,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        notional: float = 0.0,
        pnl_amount: Optional[float] = None,
        side: str = "LONG",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RealizedTrade:
        """Record a realized trade outcome."""
        closed_dt = _coerce_datetime(closed_at)
        opened_dt = _coerce_datetime(opened_at) if opened_at is not None else closed_dt
        notional_value = float(notional or 0.0)
        cash_pnl = pnl_amount

        if cash_pnl is None and notional_value:
            cash_pnl = notional_value * (float(pnl_pct) / 100.0)

        trade = RealizedTrade(
            pair=pair,
            opened_at=opened_dt,
            closed_at=closed_dt,
            entry_price=float(entry_price or 0.0),
            exit_price=float(exit_price or 0.0),
            pnl_pct=float(pnl_pct),
            notional=notional_value,
            pnl_amount=float(cash_pnl) if cash_pnl is not None else None,
            side=side,
            metadata=dict(metadata or {}),
        )
        self.realized_trades.append(trade)
        self.realized_trades.sort(key=lambda item: item.closed_at)
        return trade

    def record_closed_position(
        self,
        closed_position: Dict[str, Any],
        *,
        notional: Optional[float] = None,
        pnl_amount: Optional[float] = None,
    ) -> RealizedTrade:
        """Normalize a `RiskManager.closed_positions` entry into a realized trade."""
        position = closed_position["position"]
        action = closed_position.get("action", {})

        inferred_notional = notional
        if inferred_notional is None:
            inferred_notional = closed_position.get("notional")
        if inferred_notional is None:
            inferred_notional = getattr(position, "notional", 0.0)

        inferred_pnl = pnl_amount
        if inferred_pnl is None:
            inferred_pnl = closed_position.get("pnl_amount")

        metadata = {
            "action_type": action.get("type", ""),
            "amount_sol": getattr(position, "amount_sol", 0.0),
        }

        return self.record_trade(
            pair=getattr(position, "pair", action.get("pair", "UNKNOWN")),
            opened_at=getattr(position, "entry_time", closed_position.get("timestamp")),
            closed_at=closed_position.get("timestamp"),
            entry_price=getattr(position, "entry_price", 0.0),
            exit_price=action.get("price", getattr(position, "entry_price", 0.0)),
            pnl_pct=float(action.get("pnl_pct", 0.0) or 0.0),
            notional=float(inferred_notional or 0.0),
            pnl_amount=float(inferred_pnl) if inferred_pnl is not None else None,
            metadata=metadata,
        )

    def calculate_win_rate(self) -> float:
        """Return the percentage of closed trades that were profitable."""
        total = len(self.realized_trades)
        if total == 0:
            return 0.0
        wins = sum(1 for trade in self.realized_trades if trade.pnl_pct > 0)
        return (wins / total) * 100.0

    def daily_pnl(self) -> List[Dict[str, Any]]:
        """Aggregate realized trades into daily return and P&L buckets."""
        buckets: Dict[str, Dict[str, Any]] = {}

        for trade in self.realized_trades:
            day_key = trade.closed_at.date().isoformat()
            if day_key not in buckets:
                buckets[day_key] = {
                    "date": day_key,
                    "trade_count": 0,
                    "wins": 0,
                    "losses": 0,
                    "flats": 0,
                    "avg_trade_return_pct": 0.0,
                    "daily_return_factor": 1.0,
                    "cash_pnl_total": 0.0,
                    "cash_pnl_complete": True,
                }

            bucket = buckets[day_key]
            bucket["trade_count"] += 1
            bucket["avg_trade_return_pct"] += trade.pnl_pct
            bucket["daily_return_factor"] *= 1.0 + trade.return_decimal

            if trade.outcome == "win":
                bucket["wins"] += 1
            elif trade.outcome == "loss":
                bucket["losses"] += 1
            else:
                bucket["flats"] += 1

            if trade.pnl_amount is None:
                bucket["cash_pnl_complete"] = False
            else:
                bucket["cash_pnl_total"] += trade.pnl_amount

        running_factor = 1.0
        daily_rows: List[Dict[str, Any]] = []
        for day_key in sorted(buckets):
            bucket = buckets[day_key]
            return_pct = (bucket["daily_return_factor"] - 1.0) * 100.0
            running_factor *= bucket["daily_return_factor"]
            daily_rows.append(
                {
                    "date": day_key,
                    "trade_count": bucket["trade_count"],
                    "wins": bucket["wins"],
                    "losses": bucket["losses"],
                    "flats": bucket["flats"],
                    "realized_pnl": (
                        bucket["cash_pnl_total"]
                        if bucket["cash_pnl_complete"]
                        else None
                    ),
                    "return_pct": return_pct,
                    "avg_trade_return_pct": bucket["avg_trade_return_pct"]
                    / bucket["trade_count"],
                    "cumulative_return_pct": (running_factor - 1.0) * 100.0,
                }
            )

        return daily_rows

    def calculate_sharpe_ratio(self, risk_free_rate: float = 0.0) -> float:
        """
        Calculate annualized Sharpe ratio from aggregated daily returns.

        `risk_free_rate` should be passed as a decimal annual rate, e.g. `0.02`.
        """
        daily_returns = [row["return_pct"] / 100.0 for row in self.daily_pnl()]
        if len(daily_returns) < 2:
            return 0.0

        daily_rf = risk_free_rate / self.periods_per_year
        excess_returns = [daily_return - daily_rf for daily_return in daily_returns]
        volatility = stdev(excess_returns)
        if volatility == 0:
            return 0.0

        return (
            (sum(excess_returns) / len(excess_returns))
            / volatility
            * math.sqrt(self.periods_per_year)
        )

    def equity_curve(self) -> List[Dict[str, float]]:
        """Build a simple daily equity curve using compounded daily returns."""
        equity = self.starting_equity
        curve: List[Dict[str, float]] = []

        for row in self.daily_pnl():
            equity *= 1.0 + (row["return_pct"] / 100.0)
            curve.append(
                {
                    "date": row["date"],
                    "equity": equity,
                    "return_pct": row["return_pct"],
                }
            )

        return curve

    def calculate_max_drawdown(self) -> float:
        """Return max drawdown as a positive percentage."""
        equity_curve = self.equity_curve()
        if not equity_curve:
            return 0.0

        peak = self.starting_equity
        max_drawdown = 0.0
        for point in equity_curve:
            peak = max(peak, point["equity"])
            if peak == 0:
                continue
            drawdown = (peak - point["equity"]) / peak
            max_drawdown = max(max_drawdown, drawdown)

        return max_drawdown * 100.0

    def summary(self) -> Dict[str, Any]:
        """Return a compact performance snapshot."""
        rows = self.daily_pnl()
        realized_pnl_complete = rows and all(
            row["realized_pnl"] is not None for row in rows
        )
        realized_pnl = (
            sum(row["realized_pnl"] for row in rows) if realized_pnl_complete else None
        )

        return {
            "tracked_executions": len(self.executions),
            "realized_trades": len(self.realized_trades),
            "win_rate": self.calculate_win_rate(),
            "sharpe_ratio": self.calculate_sharpe_ratio(),
            "max_drawdown": self.calculate_max_drawdown(),
            "realized_pnl": realized_pnl,
            "daily_rows": len(rows),
        }

    def generate_daily_pnl_report(self, for_date: Optional[DateLike] = None) -> str:
        """Generate a markdown daily P&L report."""
        rows = self.daily_pnl()
        if for_date is not None:
            target_date = _coerce_date(for_date).isoformat()
            rows = [row for row in rows if row["date"] == target_date]

        title = "# Daily P&L Report"
        if not rows:
            return f"{title}\n\nNo realized trades tracked."

        realized_pnl_complete = all(row["realized_pnl"] is not None for row in rows)
        realized_pnl = (
            sum(row["realized_pnl"] for row in rows) if realized_pnl_complete else None
        )

        lines = [
            title,
            "",
            f"Period: {rows[0]['date']} -> {rows[-1]['date']}",
            "",
            "| Date | Trades | Wins | Losses | Realized P&L | Return | Cumulative |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]

        for row in rows:
            lines.append(
                "| {date} | {trade_count} | {wins} | {losses} | {pnl} | {return_pct:+.2f}% | {cum_pct:+.2f}% |".format(
                    date=row["date"],
                    trade_count=row["trade_count"],
                    wins=row["wins"],
                    losses=row["losses"],
                    pnl=_format_currency(row["realized_pnl"]),
                    return_pct=row["return_pct"],
                    cum_pct=row["cumulative_return_pct"],
                )
            )

        lines.extend(
            [
                "",
                "## Summary",
                f"- Realized trades: {sum(row['trade_count'] for row in rows)}",
                f"- Win rate: {self.calculate_win_rate():.2f}%",
                f"- Sharpe ratio: {self.calculate_sharpe_ratio():.2f}",
                f"- Max drawdown: {self.calculate_max_drawdown():.2f}%",
                f"- Realized P&L: {_format_currency(realized_pnl)}",
            ]
        )
        return "\n".join(lines)


__all__ = ["TradeExecution", "RealizedTrade", "TradingAnalytics"]
