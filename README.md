# Jupiter Sentinel

An autonomous AI-powered DeFi sentinel agent that combines multiple Jupiter APIs to detect volatility, execute trades, and manage risk — all from the terminal.

## What it does

Jupiter Sentinel is a **multi-API autonomous agent** that combines 5 Jupiter APIs in ways they weren't individually designed for:

1. **Price Oracle** — Uses Jupiter Swap V1 quotes as a real-time price feed to detect volatility spikes
2. **Volatility Scanner** — Continuously monitors token pairs and calculates rolling volatility metrics
3. **Smart Order Router** — Detects price discrepancies between routes and auto-executes arbitrage
4. **Risk Manager** — Implements trailing stop-losses and position sizing based on volatility
5. **Telegram Brain** — AI agent that receives signals, makes decisions, and reports to you via Telegram

## APIs Used

| API | Endpoint | How We Use It |
|-----|----------|---------------|
| **Swap V1** | `/swap/v1/quote`, `/swap/v1/swap` | Price oracle + execution |
| **Swap V2** | `/swap/v2/order`, `/swap/v2/execute` | Gasless swaps (with API key) |
| **Price** | Jupiter quotes as price feed | Volatility detection |
| **Tokens** | Token metadata + search | Token screening |
| **Trigger** | Limit orders, OCO, TP/SL | Automated exit strategies |

## Architecture

```
                    ┌─────────────┐
                    │  Telegram    │
                    │  Interface   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  AI Brain   │
                    │  (Decision) │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────▼───┐ ┌─────▼─────┐ ┌───▼────────┐
     │  Volatility │ │  Trade    │ │   Risk     │
     │  Scanner   │ │ Executor  │ │  Manager   │
     └────────┬───┘ └─────┬─────┘ └───┬────────┘
              │            │            │
              └────────────┼────────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────▼───┐ ┌─────▼─────┐ ┌───▼────────┐
     │  Jupiter   │ │  Jupiter  │ │  Jupiter   │
     │  Swap API  │ │  Price    │ │  Trigger   │
     └────────────┘ └───────────┘ └────────────┘
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SOLANA_PRIVATE_KEY_PATH=~/.clawd/secrets/SOLANA_SHADOW_001_KEY
export TELEGRAM_BOT_TOKEN=your_token  # optional

# Run the sentinel
python src/main.py

# Or run individual modules
python src/scanner.py     # Just scan for volatility
python src/executor.py    # Execute a single trade
python src/risk.py        # Monitor positions
```

## What Makes This "Oh" Worthy

- **Quotes as Oracle**: We use Jupiter's swap quote engine as a real-time price oracle — something it wasn't designed for but works beautifully
- **Cross-Route Arbitrage Detection**: We detect price differences between Jupiter's route plans and auto-execute when profitable
- **Volatility-Adaptive Trading**: Position sizes and stop-losses automatically adjust based on real-time volatility metrics
- **Full Autonomy**: The agent runs 24/7, makes decisions, executes trades, and reports to you — no manual intervention
- **Composable Pipeline**: Each module is independent and composable — combine them however you want

## Developer Platform Feedback

Honest feedback from building with Jupiter APIs:

**What worked well:**
- Swap V1 API is incredibly reliable and fast
- Quote engine is an excellent price oracle
- Token list is comprehensive
- No auth needed for basic usage

**What could be better:**
- Swap V2 requires API key from portal (sign-in wall) — had to fall back to V1
- No WebSocket/streaming price feeds — had to poll quotes
- Trigger API docs are portal-gated too
- Rate limiting on some endpoints without API key
- Would love a "paper trading" mode for testing strategies

## License

MIT
