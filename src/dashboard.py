"""
Jupiter Sentinel - Terminal Dashboard
Beautiful real-time dashboard using Rich library.
"""
import time
import json
import urllib.request
from datetime import datetime

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich.columns import Columns
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
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return int(resp["outAmount"]) / 1e6 / 0.001
    except:
        return 0


def get_wallet_balance():
    from .config import get_pubkey
    pubkey = get_pubkey()
    rpc_body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance", "params": [pubkey],
    }).encode()
    req = urllib.request.Request(RPC_URL, data=rpc_body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    sol = resp.get("result", {}).get("value", 0) / 1e9
    return sol


def get_pair_prices():
    prices = {}
    for input_mint, output_mint, name in SCAN_PAIRS:
        try:
            if input_mint == SOL_MINT:
                url = f"{JUPITER_SWAP_V1}/quote?inputMint={input_mint}&outputMint={output_mint}&amount=1000000&slippageBps=50"
            else:
                url = f"{JUPITER_SWAP_V1}/quote?inputMint={input_mint}&outputMint={output_mint}&amount=1000000&slippageBps=50"
            req = urllib.request.Request(url, headers=HEADERS)
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            out = int(resp["outAmount"])
            
            if output_mint == USDC_MINT:
                if input_mint == SOL_MINT:
                    prices[name] = out / 1e6 / 0.001
                else:
                    prices[name] = out / 1e6
            elif output_mint == SOL_MINT:
                sol_price = prices.get("SOL/USDC", 80)
                prices[name] = (out / 1e9) * sol_price
        except:
            prices[name] = 0
    return prices


def generate_dashboard():
    if not HAS_RICH:
        print("Install rich: pip install rich")
        return
    
    console = Console()
    
    sol_price = get_sol_price()
    sol_balance = get_wallet_balance()
    usd_value = sol_balance * sol_price
    prices = get_pair_prices()
    
    # Header
    header = Panel(
        Text.from_markup(
            f"[bold cyan]JUPITER SENTINEL[/] | [dim]Autonomous AI DeFi Agent[/]\n"
            f"[green]Wallet:[/] [bold]{sol_balance:.6f} SOL[/] ([green]${usd_value:.2f}[/]) | "
            f"[green]SOL:[/] [bold]${sol_price:.2f}[/] | "
            f"[dim]{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC[/]"
        ),
        border_style="cyan",
    )
    
    # Price table
    price_table = Table(title="Market Prices (via Jupiter Swap Quotes)", show_header=True, header_style="bold magenta")
    price_table.add_column("Pair", style="cyan")
    price_table.add_column("Price", justify="right", style="green")
    price_table.add_column("Source", style="dim")
    
    for name, price in prices.items():
        if price > 1:
            price_str = f"${price:,.2f}"
        elif price > 0.001:
            price_str = f"${price:.4f}"
        else:
            price_str = f"${price:.8f}"
        price_table.add_row(name, price_str, "Jupiter /quote")
    
    # APIs table
    api_table = Table(title="Jupiter APIs Combined", show_header=True, header_style="bold yellow")
    api_table.add_column("API", style="cyan")
    api_table.add_column("Endpoint", style="dim")
    api_table.add_column("Creative Usage", style="green")
    
    api_table.add_row("Swap V1", "/swap/v1/quote", "Real-time price oracle")
    api_table.add_row("Swap V1", "/swap/v1/swap", "Trade execution + signing")
    api_table.add_row("Route Plan", "quote.routePlan[]", "Cross-route arbitrage detection")
    api_table.add_row("Price", "Derived from quotes", "Rolling volatility tracker")
    api_table.add_row("Tokens", "Token metadata", "Automated screening")
    api_table.add_row("Trigger", "Limit orders (planned)", "Auto stop-loss / take-profit")
    
    # Features
    features = Panel(
        Text.from_markup(
            "[bold]Innovation: Quotes-as-Oracle[/]\n"
            "We repurpose Jupiter\'s swap quote engine as a multi-pair\n"
            "real-time price feed. No dedicated price API needed.\n\n"
            "[bold]Cross-Route Arbitrage[/]\n"
            "We detect price discrepancies between Jupiter\'s own\n"
            "routing options by quoting at different trade sizes.\n\n"
            "[bold]Full Autonomy[/]\n"
            "Runs 24/7, makes decisions, executes trades,\n"
            "manages risk - no human intervention."
        ),
        title="What Makes This [bold]\'Oh\'[/] Worthy",
        border_style="yellow",
    )
    
    console.print(header)
    console.print()
    console.print(price_table)
    console.print()
    console.print(api_table)
    console.print()
    console.print(features)


if __name__ == "__main__":
    generate_dashboard()
