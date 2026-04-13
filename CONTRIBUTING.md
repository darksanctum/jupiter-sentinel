# Contributing to Jupiter Sentinel

This repository is a Python codebase for experimenting with Jupiter-powered market data, execution, and trading strategies on Solana. The project is intentionally modular: core services live under `src/`, several strategies can run standalone, and newer modules are covered by unit tests in `tests/`.

## Development Setup

### Prerequisites

- Python 3.10+
- A Unix-like shell
- Optional: a Solana wallet file if you want to run live execution paths

### Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### Install dependencies

Install the runtime dependencies first:

```bash
pip install -r requirements.txt
```

Install the packages used by tests and the web dashboard:

```bash
pip install pytest fastapi uvicorn jinja2
```

Notes:

- `requirements.txt` covers the core trading modules.
- The FastAPI dashboard dependencies are documented in `README.md` but are not pinned in `requirements.txt`.
- The test suite uses `pytest`, which is also not listed in `requirements.txt`.
- `docs/API.md` documents current Jupiter auth and rate-limit expectations. The repo does not yet expose a first-class `JUP_API_KEY` setting in `src/config.py`, so contributors changing quote-heavy paths should review that document before increasing request volume.

### Wallet configuration

Most read-only modules can run without a wallet. Live execution code paths require a Solana keypair JSON file.

Set:

```bash
export SOLANA_PRIVATE_KEY_PATH=/absolute/path/to/your/keypair.json
```

Current behavior:

- `src/config.py` reads `SOLANA_PRIVATE_KEY_PATH`.
- If it is unset, the code falls back to `~/.clawd/secrets/SOLANA_SHADOW_001_KEY`.
- `TradeExecutor()` loads the keypair during initialization, so modules that instantiate it require a valid file even in some dry-run flows.

### Useful commands

Run the demo:

```bash
python demo.py
```

Run the main agent in dry-run mode:

```bash
python -m src.main
```

Run the main agent in live mode:

```bash
python -m src.main --live
```

Run the terminal dashboard:

```bash
python -m src.dashboard
```

Run the web dashboard:

```bash
python -m src.web_dashboard
```

Run the backtester:

```bash
python -m src.backtest
python -m src.backtest --data path/to/prices.csv
```

Run tests:

```bash
pytest
```

Run a focused test file while iterating:

```bash
pytest tests/test_oracle.py
pytest tests/test_backtest.py
```

## Architecture Overview

### High-level layout

- `src/config.py`: shared constants, token mints, scan pairs, paths, and wallet loading.
- `src/oracle.py`: Jupiter quote polling wrapped as a rolling `PriceFeed`.
- `src/scanner.py`: multi-pair volatility scanner that emits alert dictionaries.
- `src/executor.py`: quote, swap, sign, and broadcast flow for Jupiter swaps.
- `src/risk.py`: position sizing, stop-loss, take-profit, and trailing-stop management.
- `src/main.py`: top-level orchestrator that wires scanner, executor, risk, arbitrage, and sentiment together.
- `src/backtest.py`: historical replay engine that reuses scanner/risk concepts with simulated execution.
- `src/analytics.py`: normalized execution and realized-trade analytics.
- `src/web_dashboard.py`: mock FastAPI dashboard UI.

### Strategy and intelligence modules

These modules are currently standalone or helper-style components rather than a formal plugin system:

- `src/arbitrage.py`: simple route arbitrage detector.
- `src/cross_chain_arb.py`: newer typed cross-size route spread detector with tests.
- `src/gridbot.py`: grid trading state and trigger logic.
- `src/dca.py`: DCA simulation and reporting.
- `src/triangular.py`: triangular arbitrage scanner.
- `src/dex_intel.py`: DEX route-label analysis using Jupiter route metadata.
- `src/whale_watcher.py`: Solana RPC watcher for large exchange wallet flows.
- `src/sentiment.py`: external sentiment signals from Alternative.me and CoinGecko.

### Runtime shape

The current production-like loop is centered on `JupiterSentinel` in `src/main.py`:

1. `VolatilityScanner` polls Jupiter quotes through `PriceFeed`.
2. Large moves generate alert dictionaries.
3. `RouteArbitrage` and `RiskManager` evaluate the alert.
4. `SentimentAnalyzer` provides an extra decision input.
5. `TradeExecutor` is available for live execution when `--live` is enabled.

Important detail:

- Not every strategy module is wired into `src/main.py`.
- `gridbot`, `dca`, `triangular`, `whale_watcher`, and some intelligence modules are currently separate experiments or utilities.
- If you add a new strategy, decide explicitly whether it should stay standalone or become part of the main orchestrator.

### Data flow and state

- Most network I/O is direct `urllib.request` calls to Jupiter.
- Shared configuration comes from `src/config.py`.
- Runtime artifacts are written under `data/` and `logs/`.
- Backtesting avoids live RPC calls by replacing the executor and price feeds with historical adapters.

### Testing style

The tests in `tests/` use `pytest` and monkeypatch network boundaries instead of hitting live endpoints. That is the expected pattern for new contributions:

- patch `urllib.request.urlopen`
- keep fixtures small and deterministic
- prefer validating normalized return values over console output

## How to Add a New Strategy

There is no formal `Strategy` base class yet. The current extension pattern is module-based, with small dataclasses for state and plain Python methods for orchestration. Follow that pattern unless you are explicitly refactoring the architecture.

### 1. Create a focused module under `src/`

Add a new file such as:

```text
src/mean_reversion.py
```

Prefer this structure:

- one main class, for example `MeanReversionStrategy`
- dataclasses for state snapshots, signals, or opportunities
- small methods for quote fetching, signal generation, and action selection
- structured return values (`dict` or dataclass), not only prints

Example skeleton:

```python
from dataclasses import dataclass
from typing import Optional

from .config import HEADERS, JUPITER_SWAP_V1


@dataclass
class StrategySignal:
    pair: str
    action: str
    confidence: float
    price: float


class MeanReversionStrategy:
    def __init__(self, threshold_pct: float = 2.0):
        self.threshold_pct = threshold_pct

    def fetch_context(self, input_mint: str, output_mint: str) -> Optional[dict]:
        # Keep network I/O isolated here.
        ...

    def evaluate(self, pair_name: str, market_context: dict) -> Optional[StrategySignal]:
        # Return a signal object instead of printing-only side effects.
        ...
```

### 2. Reuse existing building blocks where possible

Before adding new infrastructure, check whether the strategy can build on:

- `PriceFeed` from `src/oracle.py` for rolling price history
- `VolatilityScanner` patterns from `src/scanner.py` for alert generation
- `RiskManager` from `src/risk.py` for entry sizing and exits
- `TradeExecutor` from `src/executor.py` for execution
- `TradingAnalytics` from `src/analytics.py` for normalized reporting
- `HistoricalBacktester` patterns from `src/backtest.py` for offline validation

Prefer composition over duplicating quote parsing or balance logic.

### 3. Decide how it should run

Choose one of these integration models:

- Standalone utility: add a `run_standalone()` or `if __name__ == "__main__"` entrypoint and keep it independent.
- Main-loop strategy: instantiate it in `src/main.py` and call it from the alert workflow or scan cycle.
- Analysis-only support module: expose reusable methods and let another module call them.

If you wire it into `src/main.py`, keep the change narrow:

- initialize the strategy in `JupiterSentinel.__init__`
- call it from `_handle_alert()` or another clearly named method
- avoid mixing strategy-specific logic directly into unrelated modules

### 4. Keep external calls isolated and configurable

Contributors should avoid scattering raw HTTP calls across many methods.

Good practice in this repo:

- isolate quote fetching or API requests in one method
- use constants from `src/config.py`
- keep parsing and decision logic separate
- return `None` or a structured failure instead of throwing on every transient API issue

If you introduce:

- a new Jupiter endpoint
- a new third-party API
- a new environment variable

also update:

- `docs/API.md`
- `README.md` if the feature is user-facing
- `src/config.py` if shared configuration is needed

### 5. Add tests at the same time

Every new strategy should ship with focused tests in `tests/`.

Recommended pattern:

- create `tests/test_<strategy>.py`
- monkeypatch network calls
- cover both the happy path and malformed-response path
- test the strategy's normalized outputs, not just side effects

Example checklist:

- valid quote produces a signal or opportunity
- invalid API payload returns `None` or an empty list
- threshold logic triggers only when expected
- route or price normalization is correct

### 6. Add a backtest or replay path when the strategy is stateful

If the strategy depends on time series behavior, add one of:

- a backtest adapter inside `src/backtest.py`
- a deterministic simulation helper in the new module
- fixtures that replay a short price series

Stateful strategies are difficult to review from live RPC behavior alone. Historical replay makes them much easier to verify.

### 7. Document the entrypoint

When the strategy is runnable by contributors, add the command to `README.md` and mention any required wallet or API setup.

## Contribution Guidelines

### Keep changes scoped

- Prefer one logical change per pull request.
- Do not mix architecture refactors with a new strategy unless the refactor is required for the feature.
- If you touch execution or risk logic, explain the behavioral impact clearly.

### Match the existing code style

- Use type hints where the surrounding module uses them.
- Prefer dataclasses for structured records.
- Keep top-level modules readable and dependency-light.
- Preserve the current `python -m src.<module>` execution pattern.

### Be careful with live trading paths

- Default to dry-run workflows while developing.
- Avoid changes that can accidentally execute swaps without an explicit `--live` path.
- Test parsing, risk, and strategy logic with mocks first.

### Be explicit about architecture changes

This repo currently has an informal strategy system. If your contribution introduces:

- a common strategy interface
- dependency injection for API clients
- async networking
- a scheduler or job runner

document that clearly in both code and `ARCHITECTURE.md`.

## Suggested Pull Request Checklist

- [ ] I can set up the project from a clean virtual environment.
- [ ] I added or updated tests for the changed behavior.
- [ ] I mocked network calls instead of depending on live Jupiter responses in tests.
- [ ] I updated `README.md` and `docs/API.md` if the feature changes setup, commands, or external integrations.
- [ ] I kept live trading behavior gated behind explicit execution paths.
