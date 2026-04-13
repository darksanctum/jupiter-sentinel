"""
Jupiter Sentinel - Terminal Dashboard
Beautiful real-time dashboard using Rich library.
"""
import time
import random
import urllib.request
import collections
from datetime import datetime
from typing import Any, Optional, Sequence

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich.columns import Columns
    from rich.align import Align
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from .config import (
    JUPITER_SWAP_V1, HEADERS, RPC_URL, SOL_MINT, USDC_MINT,
    SCAN_PAIRS, load_keypair,
)
from .resilience import request_json
from .validation import build_jupiter_quote_url


def get_sol_price() -> Optional[float]:
    try:
        url = build_jupiter_quote_url(JUPITER_SWAP_V1, SOL_MINT, USDC_MINT, 1_000_000, 10)
        req = urllib.request.Request(url, headers=HEADERS)
        resp = request_json(req, timeout=5, describe="Dashboard SOL quote")
        return int(resp["outAmount"]) / 1e6 / 0.001
    except Exception:
        return None


def get_wallet_balance() -> float:
    try:
        from .config import get_pubkey
        pubkey = get_pubkey()
        rpc_body = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance", "params": [pubkey],
        }).encode()
        req = urllib.request.Request(RPC_URL, data=rpc_body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        resp = request_json(req, timeout=5, describe="Dashboard wallet balance")
        sol = resp.get("result", {}).get("value", 0) / 1e9
        return sol
    except Exception:
        return 0.0


def ascii_plot(series: Sequence[float], height: int = 12) -> str:
    if not series:
        return "Waiting for data..."
    min_val = min(series)
    max_val = max(series)
    range_val = max_val - min_val if max_val != min_val else 1
    
    blocks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    
    result = []
    for h in range(height - 1, -1, -1):
        line = [f"${min_val + (range_val * h / (height - 1)):8.2f} |"]
        for val in series:
            norm = (val - min_val) / range_val * height
            if norm >= h + 1:
                line.append("█")
            elif norm <= h:
                line.append(" ")
            else:
                frac = norm - h
                idx = int(frac * 8)
                if idx > 7: idx = 7
                if idx < 0: idx = 0
                line.append(blocks[idx])
        result.append("".join(line))
    return "\n".join(result)


def get_header_panel(sol_balance: float, usd_value: float, current_price: float) -> Any:
    return Panel(
        Text.from_markup(
            f"[bold cyan]JUPITER SENTINEL[/] | [dim]Autonomous AI DeFi Agent[/]\n"
            f"[green]Wallet:[/] [bold]{sol_balance:.6f} SOL[/] | "
            f"[green]SOL Price:[/] [bold]${current_price:.2f}[/] | "
            f"[dim]{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC[/]"
        ),
        border_style="cyan",
    )


def get_trade_history_table() -> Any:
    trade_history = [
        {"time": "10:01:23", "pair": "SOL/USDC", "type": "BUY", "amount": "10.5 SOL", "price": "$145.20", "status": "Success"},
        {"time": "09:45:11", "pair": "JUP/USDC", "type": "SELL", "amount": "1000 JUP", "price": "$1.20", "status": "Success"},
        {"time": "09:30:00", "pair": "SOL/USDC", "type": "BUY", "amount": "5.0 SOL", "price": "$144.80", "status": "Success"},
        {"time": "09:15:22", "pair": "BONK/SOL", "type": "BUY", "amount": "1M BONK", "price": "$0.000015", "status": "Success"},
        {"time": "08:50:05", "pair": "SOL/USDC", "type": "SELL", "amount": "2.0 SOL", "price": "$142.10", "status": "Failed"},
    ]
    table = Table(title="Recent Trades History", show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Time", style="dim")
    table.add_column("Pair", style="bold")
    table.add_column("Type")
    table.add_column("Amount", justify="right")
    table.add_column("Price", justify="right", style="green")
    table.add_column("Status")
    
    for t in trade_history:
        type_style = "[bold green]BUY[/]" if t["type"] == "BUY" else "[bold red]SELL[/]"
        status_style = "[bold green]Success[/]" if t["status"] == "Success" else "[bold red]Failed[/]"
        table.add_row(t["time"], t["pair"], type_style, t["amount"], t["price"], status_style)
    return table


def get_positions_table(current_sol_price: float) -> Any:
    positions = [
        {"asset": "SOL", "amount": 15.5, "entry": 140.50, "current": current_sol_price},
        {"asset": "JUP", "amount": 5000, "entry": 1.15, "current": 1.22},
        {"asset": "BONK", "amount": 1000000, "entry": 0.000014, "current": 0.000016},
    ]
    table = Table(title="Open Positions & PnL", show_header=True, header_style="bold magenta", expand=True)
    table.add_column("Asset", style="bold")
    table.add_column("Amount", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("PnL", justify="right")
    
    for p in positions:
        entry = p["entry"]
        current = p["current"]
        pnl = (current - entry) / entry * 100
        pnl_str = f"[green]+{pnl:.2f}%[/]" if pnl >= 0 else f"[red]{pnl:.2f}%[/]"
        
        # Format amounts and prices nicely
        amount_str = f"{p['amount']:,.1f}" if p['amount'] > 100 else f"{p['amount']:.2f}"
        entry_str = f"${entry:.6f}" if entry < 0.1 else f"${entry:.2f}"
        current_str = f"${current:.6f}" if current < 0.1 else f"${current:.2f}"
        
        table.add_row(p["asset"], amount_str, entry_str, current_str, pnl_str)
    return table


def get_strategy_performance_table() -> Any:
    strategies = [
        {"name": "Mean Reversion", "win_rate": "68%", "pnl": "+$450.20", "status": "Active"},
        {"name": "Momentum", "win_rate": "55%", "pnl": "+$120.50", "status": "Active"},
        {"name": "Cross-Chain Arb", "win_rate": "92%", "pnl": "+$890.00", "status": "Active"},
        {"name": "Grid Bot", "win_rate": "N/A", "pnl": "-$15.00", "status": "Paused"},
    ]
    table = Table(title="Strategy Performance", show_header=True, header_style="bold yellow", expand=True)
    table.add_column("Strategy", style="bold")
    table.add_column("Win Rate", justify="right")
    table.add_column("Total PnL", justify="right")
    table.add_column("Status")
    
    for s in strategies:
        pnl_style = "[green]" if "+" in s["pnl"] else "[red]"
        status_style = "[green]Active[/]" if s["status"] == "Active" else "[yellow]Paused[/]"
        table.add_row(s["name"], s["win_rate"], f"{pnl_style}{s['pnl']}[/]", status_style)
    return table


def get_market_regime_panel() -> Any:
    regimes = ["Bullish Trending", "Bearish Trending", "High Vol Volatile", "Low Vol Ranging"]
    current_regime = regimes[0]  # Hardcoded or mock dynamic
    
    content = (
        f"[bold white]Current Regime:[/] [bold green]{current_regime}[/]\n\n"
        f"[dim]Confidence:[/] [cyan]87%[/]\n"
        f"[dim]Volatility (24h):[/] [yellow]High[/]\n"
        f"[dim]Trend Strength:[/] [green]Strong[/]"
    )
    return Panel(
        Text.from_markup(content),
        title="Market Regime Indicator",
        border_style="yellow"
    )


def get_profit_locked_panel(portfolio_value: float) -> Any:
    profit_locked = portfolio_value * 0.35  # mock 35% locked
    tradable = portfolio_value - profit_locked
    
    content = (
        f"[bold white]Total Balance:[/] [bold cyan]${portfolio_value:,.2f}[/]\n\n"
        f"[bold green]Profit Locked:[/] [bold]${profit_locked:,.2f}[/] (35%)\n"
        f"[bold yellow]Tradable Balance:[/] [bold]${tradable:,.2f}[/] (65%)"
    )
    return Panel(
        Text.from_markup(content),
        title="Profit Locker",
        border_style="green"
    )


def generate_layout() -> Any:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
    )
    layout["main"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=1)
    )
    layout["left"].split_column(
        Layout(name="chart", size=15),
        Layout(name="positions", size=8),
        Layout(name="trades")
    )
    layout["right"].split_column(
        Layout(name="regime", size=7),
        Layout(name="profit_locked", size=7),
        Layout(name="strategy")
    )
    return layout


def generate_dashboard() -> None:
    if not HAS_RICH:
        print("Install rich: pip install rich")
        return
    
    console = Console()
    layout = generate_layout()
    
    # Initial fetches
    initial_sol_price = get_sol_price() or 140.0
    sol_balance = get_wallet_balance()
    initial_portfolio_value = sol_balance * initial_sol_price if sol_balance > 0 else 10000.0
    
    # Pre-populate history with a random walk ending at current portfolio value
    portfolio_history = collections.deque(maxlen=80)
    simulated_value = initial_portfolio_value - 500.0
    for _ in range(80):
        simulated_value += random.uniform(-40.0, 45.0)
        portfolio_history.append(simulated_value)
    
    portfolio_history.append(initial_portfolio_value)
    
    current_price = initial_sol_price
    
    console.clear()
    
    try:
        with Live(layout, refresh_per_second=2, screen=True):
            while True:
                # Simulate price updates for the dashboard
                current_price += random.uniform(-0.1, 0.1)
                
                # Portfolio value walk
                current_portfolio_value = portfolio_history[-1] + random.uniform(-10.0, 15.0)
                portfolio_history.append(current_portfolio_value)
                
                # Update sections
                layout["header"].update(get_header_panel(sol_balance, current_portfolio_value, current_price))
                
                chart_str = ascii_plot(list(portfolio_history), height=10)
                layout["chart"].update(Panel(
                    Text(chart_str, style="cyan"), 
                    title="Live Portfolio Value (USD)", 
                    border_style="cyan"
                ))
                
                layout["positions"].update(Panel(get_positions_table(current_price), border_style="magenta"))
                layout["trades"].update(Panel(get_trade_history_table(), border_style="cyan"))
                layout["regime"].update(get_market_regime_panel())
                layout["profit_locked"].update(get_profit_locked_panel(current_portfolio_value))
                layout["strategy"].update(Panel(get_strategy_performance_table(), border_style="yellow"))
                
                time.sleep(1)
    except KeyboardInterrupt:
        console.print("[bold red]Dashboard stopped by user.[/]")

if __name__ == "__main__":
    generate_dashboard()
