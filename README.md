# ╔══════════════════════════════════════════════════════════════╗
# ║                                                             ║
# ║   ██╗███╗   ██╗███████╗██████╗  ██████╗ ████████╗██╗  ██╗  ║
# ║   ██║████╗  ██║██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝╚██╗██╔╝  ║
# ║   ██║██╔██╗ ██║█████╗  ██████╔╝██║   ██║   ██║    ╚███╔╝   ║
# ║   ██║██║╚██╗██║██╔══╝  ██╔══██╗██║   ██║   ██║    ██╔██╗   ║
# ║   ██║██║ ╚████║███████╗██║  ██║╚██████╔╝   ██║   ██╔╝ ██╗  ║
# ║   ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝  ║
# ║                                                             ║
# ║        Autonomous AI DeFi Agent for Jupiter                 ║
# ║        Built for the "Not Your Regular Bounty"              ║
# ╚══════════════════════════════════════════════════════════════╝

> What if Jupiter's swap quote engine IS the price oracle?

Jupiter Sentinel is an autonomous AI DeFi agent that discovered something unexpected:
Jupiter's `/swap/v1/quote` endpoint works as a perfect multi-pair real-time price feed.
No dedicated price API needed. We repurpose the routing engine itself as our oracle.

This agent runs 24/7, monitors 5+ token pairs, detects volatility spikes, finds
cross-route arbitrage opportunities, manages risk with trailing stops, and executes
trades — all without human intervention.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  JUPITER SENTINEL                    │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │  ORACLE   │  │ SCANNER  │  │  DEX ROUTE INTEL  │  │
│  │           │  │          │  │                   │  │
│  │ Quotes as │─▶│ Volatility│  │ 90 DEX labels    │  │
│  │ Price     │  │ Scanner  │  │ Route analysis   │  │
│  │ Feed      │  │ 5 pairs  │  │ Cross-DEX arb    │  │
│  └─────┬─────┘  └─────┬────┘  └────────┬──────────┘  │
│        │              │                │              │
│        ▼              ▼                ▼              │
│  ┌──────────────────────────────────────────────┐    │
│  │              RISK MANAGER                     │    │
│  │  Trailing stops │ Position sizing │ P&L track │    │
│  └──────────────────────┬───────────────────────┘    │
│                         │                             │
│                         ▼                             │
│  ┌──────────────────────────────────────────────┐    │
│  │           TRADE EXECUTOR                      │    │
│  │  Jupiter V1 swap │ Auto SOL wrap │ Sign + Send│    │
│  └──────────────────────────────────────────────┘    │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ GRID BOT │  │ DCA BOT  │  │  TRIANGULAR ARB   │  │
│  │          │  │          │  │                   │  │
│  │ Grid     │  │ Dollar   │  │ SOL→TKN→USDC→SOL  │  │
│  │ Trading  │  │ Cost Avg │  │ Loop detection    │  │
│  └──────────┘  └──────────┘  └───────────────────┘  │
│                                                      │
│  ┌──────────────────────────────────────────────┐    │
│  │           RICH TERMINAL DASHBOARD             │    │
│  │  Prices │ Balance │ Alerts │ Opportunities    │    │
│  └──────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

## Jupiter APIs Used

| API | Endpoint | Creative Usage |
|-----|----------|---------------|
| **Swap V1 Quote** | `/swap/v1/quote` | Real-time multi-pair price oracle |
| **Swap V1 Execute** | `/swap/v1/swap` | Trade execution with auto SOL wrapping |
| **Swap V1 DEX Labels** | `/swap/v1/program-id-to-label` | 90 DEX route mapping for arbitrage |
| **Route Plan** | `quote.routePlan[]` | Cross-route price discrepancy detection |
| **Price (derived)** | Computed from quotes | Rolling volatility tracker |

### What We Discovered

1. **Quotes-as-Oracle**: Quoting 0.001 SOL every 30 seconds gives us real-time
   prices across any pair Jupiter supports. We get price + route + impact in one call.
   This is more data than a dedicated price API would provide.

2. **Route Depth Varies by Size**: Small amounts (0.001 SOL) route through 2-3 DEXes
   for best price. Large amounts (0.5 SOL) route through 1-2 DEXes. Different sizes
   use different liquidity pools, creating detectable price discrepancies.

3. **90 DEXes Mapped**: The `/program-id-to-label` endpoint reveals Jupiter's entire
   DEX routing network — from Raydium and Orca to obscure pools like GoonFi and
   WhaleStreet. This is a treasure map for arbitrage hunters.

## Modules

| Module | Description |
|--------|-------------|
| `src/config.py` | Wallet config, API URLs, token mints, risk parameters |
| `src/oracle.py` | Price feed using swap quotes as real-time oracle |
| `src/scanner.py` | Continuous volatility monitoring across 5 pairs |
| `src/executor.py` | Full swap execution: quote → sign → broadcast |
| `src/risk.py` | Position management, trailing stops, P&L tracking |
| `src/arbitrage.py` | Cross-route price discrepancy detector |
| `src/triangular.py` | SOL→Token→USDC→SOL triangular arbitrage scanner |
| `src/dex_intel.py` | 90-DEX route intelligence with label mapping |
| `src/gridbot.py` | Grid trading strategy with configurable levels |
| `src/dca.py` | Dollar-cost averaging bot with P&L tracking |
| `src/dashboard.py` | Beautiful Rich terminal dashboard |
| `src/main.py` | Full autonomous agent orchestrator |

## Quick Start

```bash
# Install dependencies
pip install httpx solders solana base58 rich click python-dotenv fastapi uvicorn jinja2

# See all features (demo mode, no wallet needed)
python demo.py

# Launch the terminal dashboard
python -m src.dashboard

# Launch the beautiful web dashboard (FastAPI + Chart.js)
python -m src.web_dashboard

# Run the full autonomous agent (dry run mode)
python -m src.main
```

### Web Dashboard & Screenshot Instructions

We've added a professional Web Dashboard for tracking your autonomous trading bots! To view the dashboard:

1. Run `python -m src.web_dashboard`
2. Open your browser and navigate to `http://127.0.0.1:8000`
3. Take a screenshot of the dashboard to include in your project presentation! The dashboard features a professional dark theme, real-time Chart.js portfolio performance graph, active positions, trade history, and Jupiter API status monitoring.

## Demo Output

```
╭──────────────────────────────────────────────────────────────╮
│ JUPITER SENTINEL | Autonomous AI DeFi Agent                  │
│ Wallet: 0.110220 SOL ($9.03) | SOL: $81.91 | 2026-04-13 UTC │
╰──────────────────────────────────────────────────────────────╯

  Market Prices (via Jupiter Swap Quotes)
┏━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ Pair      ┃       Price ┃ Source         ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ SOL/USDC  │      $81.91 │ Jupiter /quote │
│ JUP/USDC  │     $0.1614 │ Jupiter /quote │
│ BONK/USDC │ $0.00005600 │ Jupiter /quote │
└───────────┴─────────────┴────────────────┘

DEX Route Intelligence: 90 DEXes mapped
Including: Raydium, Orca, Phoenix, Meteora, Pump.fun, 
Whirlpool, Saber, PancakeSwap, and 82 more...
```

## Innovation Highlights

### 1. Quotes-as-Oracle Pattern
```python
# Instead of calling a price API, we call the swap quote engine:
quote = get_quote(SOL, USDC, amount=1_000_000)  # 0.001 SOL
price = quote.outAmount / 1e6 / 0.001  # Derive price from quote

# We get MORE data than a price API:
# - Exact price (outAmount)
# - Which DEXes route through (routePlan)
# - Price impact at this size (priceImpactPct)
# - Slippage estimate (otherAmountThreshold)
```

### 2. Route Depth Analysis
```python
# Same pair, different sizes → different routes
0.001 SOL → 2-3 DEX hops (best price, small size)
0.005 SOL → 3 DEX hops (intermediate)
0.050 SOL → 1-2 DEX hops (direct route)
# Price discrepancies between routes = arbitrage opportunity
```

### 3. Full Autonomous Trading
```python
# The agent runs this loop 24/7:
while True:
    prices = scan_all_pairs()        # Via quote oracle
    volatilities = calculate_vol()   # Rolling window
    opportunities = find_arb()       # Cross-route + triangular
    if opportunity > threshold:
        size = calculate_position(volatility)
        execute_swap(opportunity, size)
        monitor_with_trailing_stop()
```

## Feedback on Jupiter APIs

See [FEEDBACK.md](./FEEDBACK.md) for detailed honest feedback.

**Summary**: V1 swap API is rock-solid and production-grade. The main gap is
agent onboarding — V2 requires OAuth portal sign-in, which blocks autonomous
agents from self-onboarding. Would love WebSocket price feeds and a paper
trading mode.

## Built By

**Hermes Agent** — an autonomous AI coding agent that built this entire project
in a single session, testing every module on Solana mainnet with real transactions.

No human wrote a single line of code.

## License

MIT
