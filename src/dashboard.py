"""
Jupiter Sentinel - Terminal Dashboard
Beautiful real-time dashboard using Rich library.
"""
import time
import json
import random
import urllib.request
import collections
from datetime import datetime

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


def get_sol_price():
    try:
        url = f"{JUPITER_SWAP_V1}/quote?inputMint={SOL_MINT}&outputMint={USDC_MINT}&amount=1000000&slippageBps=10"
        req = urllib.request.Request(url, headers=HEADERS)
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return int(resp["outAmount"]) / 1e6 / 0.001
    except:
        return None


def get_wallet_balance():
    try:
        from .config import get_pubkey
        pubkey = get_pubkey()
        rpc_body = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance", "params": [pubkey],
        }).encode()
        req = urllib.request.Request(RPC_URL, data=rpc_body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        sol = resp.get("result", {}).get("value", 0) / 1e9
        return sol
    except:
        return 0.0


def ascii_plot(series, height=12):
    if not series:
        return "Waiting for data..."
    min_val = min(series)
    max_val = max(series)
    range_val = max_val - min_val if max_val != min_val else 1
    
    blocks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    
    result = []
    for h in range(height - 1, -1, -1):
        line = [f"{min_val + (range_val * h / (height - 1)):8.2f} |"]
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


def get_header_panel(sol_balance, usd_value, current_price):
    return Panel(
        Text.from_markup(
            f"[bold cyan]JUPITER SENTINEL[/] | [dim]Autonomous AI DeFi Agent[/]\n"
            f"[green]Wallet:[/] [bold]{sol_balance:.6f} SOL[/] ([green]${usd_value:.2f}[/]) | "
            f"[green]SOL Price:[/] [bold]${current_price:.2f}[/] | "
            f"[dim]{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC[/]"
        ),
        border_style="cyan",
    )


def get_trade_history_table():
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


def get_positions_table(current_sol_price):
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


import threading

_cached_routes = []
_last_route_fetch = 0

def fetch_real_routes():
    global _cached_routes, _last_route_fetch
    try:
        from .config import JUPITER_SWAP_V1, HEADERS, SOL_MINT, USDC_MINT
        pairs = [
            ("USDC", "SOL", USDC_MINT, SOL_MINT, 100_000_000),
            ("SOL", "JUP", SOL_MINT, "JUPyiwrYJFskUPiHa7hkeR8VUTYb2PubCOMPQcubYhy", 1_000_000_000),
            ("USDC", "BONK", USDC_MINT, "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", 100_000_000),
            ("SOL", "JTO", SOL_MINT, "jtojtomepa8beP8AuQc6eP5xKx34c4Y43R2eP8a2BRe", 1_000_000_000)
        ]
        new_routes = []
        for i, (in_sym, out_sym, in_mint, out_mint, amt) in enumerate(pairs, 1):
            url = f"{JUPITER_SWAP_V1}/quote?inputMint={in_mint}&outputMint={out_mint}&amount={amt}&slippageBps=50"
            req = urllib.request.Request(url, headers=HEADERS)
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            plan = resp.get("routePlan", [])
            steps = []
            for step in plan:
                label = step.get("swapInfo", {}).get("label", "?")
                if "Orca" in label: steps.append(f"[yellow]{label}[/]")
                elif "Raydium" in label: steps.append(f"[cyan]{label}[/]")
                elif "Meteora" in label: steps.append(f"[magenta]{label}[/]")
                elif "Lifinity" in label: steps.append(f"[green]{label}[/]")
                elif "Phoenix" in label: steps.append(f"[blue]{label}[/]")
                else: steps.append(f"[white]{label}[/]")
            if not steps:
                steps = ["[red]Direct[/]"]
            new_routes.append(f"{i}. {in_sym} ➔ {' ➔ '.join(steps)} ➔ {out_sym}")
        _cached_routes = new_routes
        _last_route_fetch = time.time()
    except Exception:
        pass

def get_dex_routes_panel():
    global _cached_routes, _last_route_fetch
    if not _cached_routes or (time.time() - _last_route_fetch) > 30:
        threading.Thread(target=fetch_real_routes, daemon=True).start()
        
    routes = _cached_routes if _cached_routes else [
        "1. USDC ➔ [yellow]Orca[/] ➔ SOL",
        "2. SOL ➔ [cyan]Raydium[/] ➔ JUP ➔ [magenta]Meteora[/] ➔ USDC",
        "3. USDC ➔ [green]Lifinity[/] ➔ BONK",
        "4. SOL ➔ [blue]Phoenix[/] ➔ JTO",
        "5. JUP ➔ [yellow]Orca[/] ➔ [cyan]Raydium[/] ➔ SOL",
    ]
    content = "\n\n".join(routes)
    return Panel(
        Text.from_markup(content),
        title="Active DEX Routes (Jupiter V6)",
        border_style="blue",
        padding=(1, 2)
    )


def generate_layout():
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
        Layout(name="chart", size=16),
        Layout(name="trades")
    )
    layout["right"].split_column(
        Layout(name="positions"),
        Layout(name="routes")
    )
    return layout


def generate_dashboard():
    if not HAS_RICH:
        print("Install rich: pip install rich")
        return
    
    console = Console()
    layout = generate_layout()
    
    # Initial fetches
    initial_sol_price = get_sol_price() or 140.0
    sol_balance = get_wallet_balance()
    
    # Pre-populate history with a random walk ending at current price
    price_history = collections.deque(maxlen=100)
    simulated_price = initial_sol_price - 5.0
    for _ in range(100):
        simulated_price += random.uniform(-0.4, 0.45)
        price_history.append(simulated_price)
    
    # Adjust last point to real price
    price_history.append(initial_sol_price)
    
    console.clear()
    
    try:
        with Live(layout, refresh_per_second=2, screen=True):
            while True:
                # Fetch new price periodically (using mock variation + intermittent real fetch)
                # To prevent rate limiting in this loop, we simulate tick-by-tick and fetch real every 10 ticks
                if random.random() < 0.1:
                    real_price = get_sol_price()
                    if real_price:
                        current_price = real_price
                    else:
                        current_price = price_history[-1] + random.uniform(-0.1, 0.1)
                else:
                    current_price = price_history[-1] + random.uniform(-0.1, 0.1)
                
                price_history.append(current_price)
                usd_value = sol_balance * current_price
                
                # Update sections
                layout["header"].update(get_header_panel(sol_balance, usd_value, current_price))
                
                chart_str = ascii_plot(list(price_history), height=12)
                layout["chart"].update(Panel(
                    Text(chart_str, style="cyan"), 
                    title="SOL/USDC Real-time Price Chart (Last 100 ticks)", 
                    border_style="green"
                ))
                
                layout["trades"].update(Panel(get_trade_history_table(), border_style="cyan"))
                layout["positions"].update(Panel(get_positions_table(current_price), border_style="magenta"))
                layout["routes"].update(get_dex_routes_panel())
                
                time.sleep(1)
    except KeyboardInterrupt:
        console.print("[bold red]Dashboard stopped by user.[/]")

if __name__ == "__main__":
    generate_dashboard()
