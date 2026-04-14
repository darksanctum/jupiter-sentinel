#!/usr/bin/env python3
"""
Full-system deterministic demo runner for Jupiter Sentinel.

This script is purpose-built for judges: a single command, no wallet, no
network, visually rich terminal output, and a fixed end-to-end narrative:

banner -> scanner -> volatility alert -> risk review -> simulated execution ->
position monitoring -> protected stop-loss -> profit lock -> final summary
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from src.ascii_charts import render_candlesticks, render_equity_curve  # noqa: E402
from src.config import MAX_POSITION_USD, STOP_LOSS_BPS, TAKE_PROFIT_BPS  # noqa: E402


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


class Tone:
    """ANSI style helpers."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[38;5;203m"
    GREEN = "\033[38;5;84m"
    YELLOW = "\033[38;5;221m"
    BLUE = "\033[38;5;81m"
    MAGENTA = "\033[38;5;213m"
    CYAN = "\033[38;5;117m"
    WHITE = "\033[38;5;231m"
    SLATE = "\033[38;5;246m"


@dataclass(frozen=True)
class MarketPair:
    name: str
    route: str
    prices: list[float]
    liquidity_millions: float

    @property
    def current_price(self) -> float:
        return self.prices[-1]

    @property
    def change_pct(self) -> float:
        return ((self.prices[-1] / self.prices[0]) - 1.0) * 100

    @property
    def volatility_pct(self) -> float:
        returns = []
        for previous, current in zip(self.prices, self.prices[1:]):
            returns.append(((current / previous) - 1.0) * 100)
        return statistics.pstdev(returns) if returns else 0.0


@dataclass(frozen=True)
class PositionTick:
    label: str
    price: float


def supports_color(stream: Any) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, *styles: str, enabled: bool = True) -> str:
    if not enabled or not styles:
        return text
    return "".join(styles) + text + Tone.RESET


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def fmt_price(value: float) -> str:
    if value >= 100:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:,.8f}"


def fmt_pct(value: float, signed: bool = True) -> str:
    if signed:
        return f"{value:+.2f}%"
    return f"{value:.2f}%"


def sparkline(values: Sequence[float]) -> str:
    if not values:
        return ""
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return SPARK_BLOCKS[0] * len(values)
    parts = []
    for value in values:
        scale = (value - low) / (high - low)
        index = min(len(SPARK_BLOCKS) - 1, max(0, round(scale * (len(SPARK_BLOCKS) - 1))))
        parts.append(SPARK_BLOCKS[index])
    return "".join(parts)


def bar(value: float, maximum: float, width: int = 16) -> str:
    if maximum <= 0:
        return " " * width
    filled = max(0, min(width, round((value / maximum) * width)))
    return "█" * filled + "░" * (width - filled)


def banner_lines() -> list[str]:
    return [
        "     ██╗██╗   ██╗██████╗ ██╗████████╗███████╗██████╗ ",
        "     ██║██║   ██║██╔══██╗██║╚══██╔══╝██╔════╝██╔══██╗",
        "     ██║██║   ██║██████╔╝██║   ██║   █████╗  ██████╔╝",
        "██   ██║██║   ██║██╔═══╝ ██║   ██║   ██╔══╝  ██╔══██╗",
        "╚█████╔╝╚██████╔╝██║     ██║   ██║   ███████╗██║  ██║",
        " ╚════╝  ╚═════╝ ╚═╝     ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝",
        " ",
        "███████╗███████╗███╗   ██╗████████╗██╗███╗   ██╗███████╗██╗     ",
        "██╔════╝██╔════╝████╗  ██║╚══██╔══╝██║████╗  ██║██╔════╝██║     ",
        "███████╗█████╗  ██╔██╗ ██║   ██║   ██║██╔██╗ ██║█████╗  ██║     ",
        "╚════██║██╔══╝  ██║╚██╗██║   ██║   ██║██║╚██╗██║██╔══╝  ██║     ",
        "███████║███████╗██║ ╚████║   ██║   ██║██║ ╚████║███████╗███████╗",
        "╚══════╝╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝",
    ]


def render_banner(color: bool) -> str:
    palette = [Tone.CYAN, Tone.BLUE, Tone.MAGENTA, Tone.YELLOW]
    lines = []
    for index, line in enumerate(banner_lines()):
        tone = palette[index % len(palette)]
        lines.append(paint(line, Tone.BOLD, tone, enabled=color))

    footer = "AUTONOMOUS JUPITER AGENT | FULL DEMO MODE | ZERO WALLET REQUIRED"
    lines.append(paint(footer, Tone.BOLD, Tone.WHITE, enabled=color))
    return "\n".join(lines)


def section(title: str, subtitle: str, color: bool, tone: str = Tone.CYAN) -> str:
    heading = paint(title, Tone.BOLD, tone, enabled=color)
    detail = paint(subtitle, Tone.SLATE, enabled=color)
    rule = paint("─" * 84, tone, enabled=color)
    return f"\n{heading}\n{detail}\n{rule}"


def box(title: str, rows: Iterable[str], color: bool, tone: str = Tone.CYAN) -> str:
    body = [strip_ansi(row) for row in rows]
    width = max(len(strip_ansi(title)), *(len(row) for row in body))
    top = f"╭─ {title} " + "─" * max(0, width - len(title) - 1) + "╮"
    bottom = "╰" + "─" * (width + 2) + "╯"
    lines = [paint(top, tone, enabled=color)]
    for row in body:
        lines.append(paint(f"│ {row.ljust(width)} │", tone, enabled=color))
    lines.append(paint(bottom, tone, enabled=color))
    return "\n".join(lines)


def to_candles(values: Sequence[float]) -> list[dict[str, float]]:
    if len(values) < 2:
        return []
    candles = []
    for previous, current in zip(values, values[1:]):
        high = max(previous, current) * 1.009
        low = min(previous, current) * 0.991
        candles.append(
            {
                "open": previous,
                "high": high,
                "low": low,
                "close": current,
            }
        )
    return candles


def build_demo_payload() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    wallet_sol = 3.876543
    sol_price = 182.45
    wallet_usd = wallet_sol * sol_price

    pairs = [
        MarketPair(
            name="SOL/USDC",
            route="Orca -> Phoenix",
            prices=[181.92, 182.01, 182.11, 182.08, 182.24, 182.33, 182.45],
            liquidity_millions=412.0,
        ),
        MarketPair(
            name="JUP/USDC",
            route="Meteora -> Orca",
            prices=[0.9360, 0.9390, 0.9415, 0.9465, 0.9485, 0.9510, 0.9555],
            liquidity_millions=87.0,
        ),
        MarketPair(
            name="BONK/USDC",
            route="Raydium -> Phoenix",
            prices=[0.0000281, 0.0000283, 0.0000284, 0.0000287, 0.0000292, 0.0000298, 0.0000307],
            liquidity_millions=54.0,
        ),
        MarketPair(
            name="WIF/USDC",
            route="Meteora DLMM -> Orca Whirlpool",
            prices=[2.218, 2.231, 2.244, 2.269, 2.341, 2.416, 2.487],
            liquidity_millions=66.0,
        ),
    ]

    signal = max(pairs, key=lambda pair: pair.change_pct)
    entry_price = signal.current_price
    stop_loss_pct = STOP_LOSS_BPS / 10000
    take_profit_pct = TAKE_PROFIT_BPS / 10000
    protected_stop_pct = 0.04
    trade_budget_usd = MAX_POSITION_USD
    trade_amount_sol = trade_budget_usd / sol_price
    token_quantity = trade_budget_usd / entry_price

    monitor_ticks = [
        PositionTick("T+00", 2.521),
        PositionTick("T+01", 2.578),
        PositionTick("T+02", 2.644),
        PositionTick("T+03", 2.706),
        PositionTick("T+04", 2.666),
        PositionTick("T+05", 2.621),
        PositionTick("T+06", 2.585),
    ]

    remaining_qty = token_quantity
    realized_usd = 0.0
    highest_price = entry_price
    protected_stop = entry_price * (1 - stop_loss_pct)
    profit_locked = False
    partial_profit_usd = 0.0
    stop_exit_profit_usd = 0.0
    monitor_rows = []
    equity_curve = [wallet_usd]

    for tick in monitor_ticks:
        highest_price = max(highest_price, tick.price)
        pnl_pct = ((tick.price / entry_price) - 1.0) * 100
        action = "tracking"

        if not profit_locked and pnl_pct >= 8.0:
            locked_qty = remaining_qty / 2
            partial_profit_usd = (tick.price - entry_price) * locked_qty
            realized_usd += partial_profit_usd
            remaining_qty -= locked_qty
            protected_stop = entry_price * (1 + protected_stop_pct)
            profit_locked = True
            action = "partial take-profit / stop tightened"

        unrealized_usd = (tick.price - entry_price) * remaining_qty
        equity_curve.append(wallet_usd + realized_usd + unrealized_usd)

        if profit_locked and tick.price <= protected_stop:
            stop_exit_profit_usd = (tick.price - entry_price) * remaining_qty
            realized_usd += stop_exit_profit_usd
            action = "protected stop-loss triggered"
            remaining_qty = 0.0
            monitor_rows.append(
                {
                    "label": tick.label,
                    "price": tick.price,
                    "pnl_pct": pnl_pct,
                    "highest": highest_price,
                    "stop": protected_stop,
                    "action": action,
                }
            )
            break

        monitor_rows.append(
            {
                "label": tick.label,
                "price": tick.price,
                "pnl_pct": pnl_pct,
                "highest": highest_price,
                "stop": protected_stop,
                "action": action,
            }
        )

    locked_profit_usd = realized_usd * 0.60
    locked_profit_sol = locked_profit_usd / sol_price
    ending_wallet_sol = wallet_sol + (realized_usd / sol_price)
    tradable_sol = ending_wallet_sol - locked_profit_sol

    return {
        "timestamp": now.isoformat(),
        "wallet": {
            "mode": "demo",
            "address": "demo-wallet-redacted",
            "sol": wallet_sol,
            "sol_price": sol_price,
            "usd_value": wallet_usd,
        },
        "pairs": pairs,
        "signal": {
            "pair": signal.name,
            "route": signal.route,
            "entry_price": entry_price,
            "change_pct": signal.change_pct,
            "volatility_pct": signal.volatility_pct,
            "liquidity_millions": signal.liquidity_millions,
        },
        "risk": {
            "position_usd": trade_budget_usd,
            "amount_sol": trade_amount_sol,
            "token_qty": token_quantity,
            "max_position_pct": (trade_budget_usd / wallet_usd) * 100,
            "stop_loss_pct": stop_loss_pct * 100,
            "take_profit_pct": take_profit_pct * 100,
            "protected_stop_pct": protected_stop_pct * 100,
            "confidence": 93,
            "slippage_bps": 75,
        },
        "execution": {
            "status": "filled-demo",
            "side": "BUY",
            "route": signal.route,
            "expected_fill": entry_price,
            "txid": "SIM-JUP-7FK29QW7MXA1",
        },
        "monitor": {
            "rows": monitor_rows,
            "equity_curve": equity_curve,
            "partial_profit_usd": partial_profit_usd,
            "stop_exit_profit_usd": stop_exit_profit_usd,
            "stop_price": protected_stop,
            "exit_price": monitor_rows[-1]["price"],
        },
        "profit_lock": {
            "realized_usd": realized_usd,
            "locked_usd": locked_profit_usd,
            "locked_sol": locked_profit_sol,
        },
        "portfolio": {
            "start_sol": wallet_sol,
            "start_usd": wallet_usd,
            "end_sol": ending_wallet_sol,
            "end_usd": wallet_usd + realized_usd,
            "tradable_sol": tradable_sol,
            "closed_positions": 1,
            "open_positions": 0,
            "win_rate_pct": 100.0,
        },
    }


def scan_table(pairs: Sequence[MarketPair], color: bool) -> str:
    lines = []
    max_vol = max(pair.volatility_pct for pair in pairs)
    for pair in pairs:
        tone = Tone.GREEN if pair.change_pct >= 0 else Tone.RED
        change = paint(f"{fmt_pct(pair.change_pct):>8}", Tone.BOLD, tone, enabled=color)
        lines.append(
            f"{pair.name:10s}  {fmt_price(pair.current_price):>12s}  "
            f"{change}  vol {pair.volatility_pct:>4.2f}%  "
            f"{bar(pair.volatility_pct, max_vol)}  {sparkline(pair.prices)}"
        )
    return "\n".join(lines)


def risk_rows(payload: dict[str, Any]) -> list[str]:
    wallet = payload["wallet"]
    signal = payload["signal"]
    risk = payload["risk"]
    return [
        f"Wallet mode            DEMO / read-only / no signing",
        f"Signal                 {signal['pair']} momentum burst {fmt_pct(signal['change_pct'])}",
        f"Capital at risk        ${risk['position_usd']:.2f} ({risk['max_position_pct']:.2f}% of portfolio)",
        f"Budget                 {risk['amount_sol']:.6f} SOL -> {risk['token_qty']:.4f} units",
        f"Hard stop-loss         -{risk['stop_loss_pct']:.2f}%  |  Take-profit +{risk['take_profit_pct']:.2f}%",
        f"Initial fill           {fmt_price(signal['entry_price'])}  |  Slippage cap {risk['slippage_bps']} bps",
        f"Liquidity backing      ${signal['liquidity_millions']:.0f}M routed via {signal['route']}",
        f"Risk verdict           APPROVED with confidence {risk['confidence']}/100",
        f"Portfolio snapshot     {wallet['sol']:.6f} SOL  (${wallet['usd_value']:.2f})",
    ]


def monitor_table(rows: Sequence[dict[str, Any]], color: bool) -> str:
    output = []
    for row in rows:
        pnl_tone = Tone.GREEN if row["pnl_pct"] >= 0 else Tone.RED
        pnl_text = paint(f"{row['pnl_pct']:+6.2f}%", Tone.BOLD, pnl_tone, enabled=color)
        action = row["action"]
        if "stop-loss" in action:
            action = paint(action, Tone.BOLD, Tone.RED, enabled=color)
        elif "partial" in action:
            action = paint(action, Tone.BOLD, Tone.YELLOW, enabled=color)
        else:
            action = paint(action, Tone.SLATE, enabled=color)
        output.append(
            f"{row['label']:5s}  {fmt_price(row['price']):>10s}  "
            f"{pnl_text}  "
            f"high {fmt_price(row['highest']):>10s}  stop {fmt_price(row['stop']):>10s}  {action}"
        )
    return "\n".join(output)


def emit(text: str, delay: float = 0.0) -> None:
    print(text)
    if delay > 0:
        time.sleep(delay)


def play_demo(payload: dict[str, Any], *, color: bool, animate: bool) -> None:
    chart_delay = 0.10 if animate else 0.0

    emit(render_banner(color), chart_delay)
    emit(
        section(
            "1. Demo Boot",
            "The judge runner starts in safe deterministic mode before any market logic begins.",
            color,
            tone=Tone.CYAN,
        )
    )
    emit(
        box(
            "SYSTEM STATUS",
            [
                "Judges mode: deterministic mock market stream",
                "Wallet mode: no private key, no RPC, no live Jupiter calls",
                "Pipeline: scanner -> risk -> executor -> monitor -> profit locker",
                f"Timestamp: {payload['timestamp']}",
            ],
            color=color,
            tone=Tone.CYAN,
        ),
        chart_delay,
    )

    emit(
        section(
            "2. Scanner Warmup",
            "Mocked market tape boots across multiple pairs using a deterministic quote stream.",
            color,
            tone=Tone.CYAN,
        )
    )
    emit(scan_table(payload["pairs"], color), chart_delay)

    signal = payload["signal"]
    candidate = next(pair for pair in payload["pairs"] if pair.name == signal["pair"])
    spike_chart = render_candlesticks(
        to_candles(candidate.prices),
        width=max(6, len(candidate.prices) - 1),
        height=10,
        title=f"{signal['pair']} volatility ignition",
    )
    emit(strip_ansi(spike_chart) if not color else spike_chart, chart_delay)

    emit(
        section(
            "3. Volatility Spike Detected",
            "The scanner escalates the strongest move and hands it to the decision stack.",
            color,
            tone=Tone.MAGENTA,
        )
    )
    emit(
        box(
            "ALERT",
            [
                f"Pair                   {signal['pair']}",
                f"Price burst            {fmt_pct(signal['change_pct'])} in 7 mocked ticks",
                f"Live route             {signal['route']}",
                f"Rolling volatility     {signal['volatility_pct']:.2f}%  |  Severity HIGH",
                "Why it matters         Fast route compression with rising liquidity",
            ],
            color=color,
            tone=Tone.MAGENTA,
        ),
        chart_delay,
    )

    emit(
        section(
            "4. Risk Manager Evaluation",
            "Position sizing, hard limits, and trade approval fire before anything executes.",
            color,
            tone=Tone.YELLOW,
        )
    )
    emit(box("RISK ENGINE", risk_rows(payload), color=color, tone=Tone.YELLOW), chart_delay)

    execution = payload["execution"]
    risk = payload["risk"]
    emit(
        section(
            "5. Simulated Trade Execution",
            "Execution is filled in demo mode with a fake transaction ID and the exact route context.",
            color,
            tone=Tone.GREEN,
        )
    )
    emit(
        box(
            "EXECUTION",
            [
                f"Order                  {execution['side']} {payload['signal']['pair']}",
                f"Budget                 {risk['amount_sol']:.6f} SOL (${risk['position_usd']:.2f})",
                f"Expected fill          {fmt_price(execution['expected_fill'])}",
                f"Route                  {execution['route']}",
                f"Transaction            {execution['txid']}",
                "Status                 FILLED (demo only, no wallet needed)",
            ],
            color=color,
            tone=Tone.GREEN,
        ),
        chart_delay,
    )

    emit(
        section(
            "6. Position Monitoring",
            "The open position is tracked tick-by-tick while the stop evolves with price action.",
            color,
            tone=Tone.BLUE,
        )
    )
    emit(monitor_table(payload["monitor"]["rows"], color), chart_delay)

    equity_chart = render_equity_curve(
        payload["monitor"]["equity_curve"],
        width=len(payload["monitor"]["equity_curve"]),
        height=8,
        title="Portfolio equity during trade",
    )
    emit(strip_ansi(equity_chart) if not color else equity_chart, chart_delay)

    emit(
        section(
            "7. Stop-Loss Trigger",
            "After partial profits are secured, the tightened protective stop closes the runner automatically.",
            color,
            tone=Tone.RED,
        )
    )
    emit(
        box(
            "STOP EVENT",
            [
                f"Protected stop         {fmt_price(payload['monitor']['stop_price'])}",
                f"Exit price             {fmt_price(payload['monitor']['exit_price'])}",
                "Mechanism              Adaptive stop-loss moved above entry after strength",
                "Outcome                Position closed green on the retrace",
            ],
            color=color,
            tone=Tone.RED,
        ),
        chart_delay,
    )

    emit(
        section(
            "8. Profit Locker",
            "Realized gains are sequestered so the next trade cannot give them back.",
            color,
            tone=Tone.MAGENTA,
        )
    )
    emit(
        box(
            "LOCKED PROFIT",
            [
                f"Partial take-profit    +${payload['monitor']['partial_profit_usd']:.4f}",
                f"Stop-close profit      +${payload['monitor']['stop_exit_profit_usd']:.4f}",
                f"Total realized PnL     +${payload['profit_lock']['realized_usd']:.4f}",
                f"Locked to vault        {payload['profit_lock']['locked_sol']:.6f} SOL  (${payload['profit_lock']['locked_usd']:.4f})",
                "Vault behavior         Locked funds are removed from tradable capital",
            ],
            color=color,
            tone=Tone.MAGENTA,
        ),
        chart_delay,
    )

    portfolio = payload["portfolio"]
    emit(
        section(
            "9. Final Portfolio Summary",
            "The full demo cycle ends flat, protected, and ready for the next scan.",
            color,
            tone=Tone.WHITE,
        )
    )
    emit(
        box(
            "PORTFOLIO",
            [
                f"Starting equity        {portfolio['start_sol']:.6f} SOL  (${portfolio['start_usd']:.2f})",
                f"Ending equity          {portfolio['end_sol']:.6f} SOL  (${portfolio['end_usd']:.2f})",
                f"Tradable balance       {portfolio['tradable_sol']:.6f} SOL",
                f"Open positions         {portfolio['open_positions']}",
                f"Closed positions       {portfolio['closed_positions']}  |  Win rate {portfolio['win_rate_pct']:.0f}%",
                "Demo integrity         No wallet, no RPC, no market risk, no randomness",
            ],
            color=color,
            tone=Tone.WHITE,
        )
    )


def render_plain_summary(payload: dict[str, Any], color: bool) -> str:
    pairs_block = scan_table(payload["pairs"], color)
    monitor_block = monitor_table(payload["monitor"]["rows"], color)
    return "\n".join(
        [
            render_banner(color),
            "",
            box(
                "SYSTEM STATUS",
                [
                    "Judges mode: deterministic mock market stream",
                    "Wallet mode: no private key, no RPC, no live Jupiter calls",
                    "Pipeline: scanner -> risk -> executor -> monitor -> profit locker",
                ],
                color=color,
                tone=Tone.CYAN,
            ),
            section("Scanner", "Mocked quotes across four pairs.", color, tone=Tone.CYAN),
            pairs_block,
            section("Risk", "Approved demo trade plan.", color, tone=Tone.YELLOW),
            box("RISK ENGINE", risk_rows(payload), color=color, tone=Tone.YELLOW),
            section("Monitor", "Position lifecycle through protected stop.", color, tone=Tone.BLUE),
            monitor_block,
            section("Final Summary", "Portfolio state after the full demo run.", color, tone=Tone.WHITE),
            box(
                "PORTFOLIO",
                [
                    f"Ending equity          {payload['portfolio']['end_sol']:.6f} SOL  (${payload['portfolio']['end_usd']:.2f})",
                    f"Locked to vault        {payload['profit_lock']['locked_sol']:.6f} SOL",
                    f"Realized PnL           +${payload['profit_lock']['realized_usd']:.4f}",
                    "Demo integrity         No wallet, no RPC, no market risk, no randomness",
                ],
                color=color,
                tone=Tone.WHITE,
            ),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the full Jupiter Sentinel judge demo with deterministic mocked data."
    )
    parser.add_argument("--json", action="store_true", help="Emit the structured demo payload as JSON.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    parser.add_argument("--no-animate", action="store_true", help="Disable staged section delays.")
    args = parser.parse_args(argv)

    payload = build_demo_payload()
    color = supports_color(sys.stdout) and not args.no_color
    animate = supports_color(sys.stdout) and not args.no_animate and not args.json

    if args.json:
        import json

        serializable = {
            **payload,
            "pairs": [
                {
                    "name": pair.name,
                    "route": pair.route,
                    "prices": pair.prices,
                    "liquidity_millions": pair.liquidity_millions,
                    "current_price": pair.current_price,
                    "change_pct": pair.change_pct,
                    "volatility_pct": pair.volatility_pct,
                }
                for pair in payload["pairs"]
            ],
        }
        print(json.dumps(serializable, indent=2))
        return 0

    play_demo(payload, color=color, animate=animate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
