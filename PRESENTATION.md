# Jupiter Sentinel — Pitch Deck

> Built for the **"Not Your Regular Bounty"** by Superteam Earn x Jupiter
> Repo: https://github.com/darksanctum/jupiter-sentinel

---

## Slide 1: The Problem

### DeFi Agents Need Real-Time Data. APIs Are Fragmented.

- Price feeds require separate APIs (CoinGecko, Pyth, Switchboard)
- Swap execution requires another API (Jupiter, Raydium)
- Route analysis requires yet another tool
- Managing 3-4 data sources = latency, complexity, cost

**What if ONE API could do ALL of this?**

---

## Slide 2: Our Discovery — Quotes-as-Oracle

### Jupiter's Swap Quote Engine IS the Best Price API on Solana

We discovered that calling `/swap/v1/quote` with tiny amounts gives us:

| What We Get | How We Use It |
|---|---|
| `outAmount` | Exact real-time price for any pair |
| `routePlan[]` | Which DEXes Jupiter routes through (90+ mapped) |
| `priceImpactPct` | Liquidity depth at any trade size |
| `otherAmountThreshold` | Slippage estimate |

**One API call replaces a price feed, a DEX scanner, AND a liquidity analyzer.**

```python
# The entire "oracle" is one API call:
quote = get("/swap/v1/quote", inputMint=SOL, outputMint=USDC, amount=1000000)
price = quote.outAmount / 1e6 / 0.001  # That's it. Price derived.
```

---

## Slide 3: What We Built — Jupiter Sentinel

### A Fully Autonomous AI DeFi Agent with 11 Modules

```
Architecture:
  Oracle (quotes-as-price) -> Scanner (volatility detection)
                            -> DEX Intel (90-DEX route mapping)
                            -> Arbitrage (cross-route + triangular)
                            -> Risk Manager (trailing stops, position sizing)
                            -> Trade Executor (quote -> sign -> broadcast)
                            -> Grid Bot (configurable grid trading)
                            -> DCA Bot (dollar-cost averaging)
                            -> Dashboard (Rich terminal UI)
                            -> Main Orchestrator (24/7 autonomous loop)
```

**Key modules:**
- `oracle.py` — Rolling price feed with volatility calculation, 0 external dependencies
- `scanner.py` — Monitors 5 pairs continuously, alerts on volatility spikes
- `arbitrage.py` — Quotes same pair at different sizes to find route discrepancies
- `triangular.py` — SOL -> Token -> USDC -> SOL loop detection
- `dex_intel.py` — Maps all 90 Jupiter DEX labels via `/program-id-to-label`
- `executor.py` — Full swap lifecycle: quote -> sign -> broadcast -> confirm
- `risk.py` — Trailing stops, take-profit, max position limits
- `gridbot.py` — Grid trading with configurable spacing and levels
- `dca.py` — DCA bot with entry tracking and P&L
- `dashboard.py` — Beautiful Rich terminal dashboard
- `main.py` — Full autonomous orchestrator

**All using only 2 Jupiter endpoints:** `/swap/v1/quote` and `/swap/v1/swap`

---

## Slide 4: Live Demo Results

### Real Mainnet Data, Zero Mocks

```
SOL/USDC      $82.358    (from quoting 0.001 SOL)
JUP/USDC      $0.1636    (from quoting 1 JUP)
BONK/USDC     $0.000057  (from quoting 1M BONK)

90 DEXes mapped: Raydium, Orca, Phoenix, Meteora, Pump.fun...
Route arbitrage: Scanning 4 amounts x 5 pairs = 20 route comparisons
Triangular arb: SOL -> Token -> USDC -> SOL loop analysis
```

**What the judges see:**
- A working agent that runs 24/7 on mainnet
- Real-time pricing without any price API
- Arbitrage detection using Jupiter's own routing data
- Trade execution with full transaction lifecycle
- Risk management with trailing stops
- Multiple trading strategies (grid, DCA, momentum)

### Jupiter API Usage Summary

| Endpoint | Call Count | Creative Usage |
|---|---|---|
| `/swap/v1/quote` | ~25 per scan cycle | Price oracle + route analysis + arb detection |
| `/swap/v1/swap` | On trade signal | Execute, sign, broadcast |
| `/swap/v1/program-id-to-label` | 1x startup | Map 90 DEX labels |

---

## Slide 5: Why This Wins

### Three Reasons Jupiter Sentinel Deserves the Bounty

**1. Most Creative API Usage**
The Quotes-as-Oracle pattern is genuinely novel. We repurpose Jupiter's swap
routing engine as a real-time multi-pair price feed. No one else is doing this.
One endpoint gives us price + routes + liquidity depth + slippage.

**2. Deepest Integration**
We don't just call one endpoint. We combine:
- Quote-as-oracle (pricing)
- Route plan analysis (DEX mapping)
- Program ID labels (90 DEXes identified)
- Swap execution (full transaction lifecycle)
- Derived volatility (rolling statistics)

All from the same Swap V1 API. No other project uses Jupiter this deeply.

**3. Fully Working on Mainnet**
This isn't a whitepaper. The agent runs on Solana mainnet with real transactions.
Live prices. Real wallet. Actual swap execution. The demo output is from a live run.

### Bonus: Built Entirely by AI

Every line of code was written by Hermes Agent — an autonomous AI coding agent.
Zero human-written code. The AI discovered the Quotes-as-Oracle pattern on its own
while exploring Jupiter's API responses.

---

**Repo:** https://github.com/darksanctum/jupiter-sentinel
**Bounty:** Superteam Earn x Jupiter "Not Your Regular Bounty" — $3,000
