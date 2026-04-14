# Contributing to Jupiter Sentinel V2

First off, thank you for considering contributing to Jupiter Sentinel! This guide will help you get started with the V2 architecture.

## Development Setup

To set up your local development environment:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-org/jupiter-sentinel.git
   cd jupiter-sentinel
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install dependencies:**
   Install the required dependencies from `requirements.txt`:
   ```bash
   pip install -r requirements.txt
   ```
   
   Dependencies include:
   - `httpx>=0.27.0`
   - `solders>=0.21.0`
   - `solana>=0.34.0`
   - `base58>=2.1.1`
   - `rich>=13.0.0`
   - `click>=8.1.0`
   - `python-dotenv>=1.0.0`
   - `numpy>=1.26.0`
   - `scipy>=1.12.0`

4. **Environment Variables:**
   Copy `.env.example` to `.env` and fill in your RPC URLs and other required configurations.

## Code Style Guide

- **Language:** Python 3.10+
- **Formatting:** Use `black` for code formatting.
- **Linting:** Use `flake8` and `mypy`.
- **Type Hints:** All new code MUST include Python type hints.
- **Docstrings:** Use Google-style docstrings for all modules, classes, and functions.
- **Naming Conventions:**
  - `snake_case` for variables and functions.
  - `CamelCase` for classes.
  - `UPPER_CASE` for constants.

## Testing Requirements

- **All PRs must pass tests.** No exceptions.
- Add unit tests for all new features and bug fixes in the `tests/` directory.
- Run tests locally before submitting a PR:
  ```bash
  pytest tests/
  ```
- Ensure test coverage remains high (aim for >80%).

## PR Review Checklist

Before submitting a Pull Request, please ensure you have completed the following:

- [ ] Code follows the established Code Style Guide.
- [ ] Type hints and docstrings are added/updated.
- [ ] All tests pass locally (`pytest tests/`).
- [ ] New tests are added for new features or bug fixes.
- [ ] Documentation is updated if necessary.
- [ ] PR title is descriptive and follows conventional commits (e.g., `feat: add new oracle module`).
- [ ] PR description explains the "Why" and "What" of the changes.

## Module-specific Development Notes

When working on specific modules, please keep these considerations in mind:

### Core Modules
- **`main.py`**: Entry point for the application. Keep it clean and focused on initialization and orchestration.
- **`config.py`**: Configuration management. Add new settings here and ensure they are loaded from environment variables where appropriate.
- **`state_manager.py`**: Handles application state persistence. Ensure any new state is serializable.
- **`monitoring.py`**: System health and performance monitoring.
- **`resilience.py`**: Fallback mechanisms and error recovery logic.
- **`rate_limiter.py`**: Manages API rate limits. Update limits if upstream APIs change.
- **`validation.py`**: Input and data validation logic.

### Trading & Execution
- **`autotrader.py`**: Core automated trading logic. Extremely sensitive; requires rigorous testing.
- **`live_trader.py`**: Live execution engine.
- **`executor.py`**: Transaction execution and signing. Ensure all transactions are correctly simulated before execution.
- **`jupiter_limits.py`**: Jupiter-specific limit order handling.

### Strategies
- **`arbitrage.py`**: General arbitrage logic.
- **`cross_chain_arb.py` / `cross_chain_arbitrage.py`**: Logic for cross-chain arbitrage opportunities. Be mindful of bridge latencies.
- **`dca.py` / `smart_dca.py`**: Dollar Cost Averaging strategies.
- **`gridbot.py`**: Grid trading bot implementation.
- **`multi_timeframe.py`**: Multi-timeframe analysis for strategies.
- **`triangular.py`**: Triangular arbitrage implementation.

### Analysis & Intelligence
- **`analytics.py`**: Trading performance analytics and metrics generation.
- **`correlation_tracker.py`**: Tracks asset price correlations.
- **`dex_intel.py`**: DEX liquidity and volume intelligence.
- **`microstructure.py`**: Market microstructure analysis (orderbook depth, tick-level data).
- **`oracle.py`**: Price oracle integrations. Must handle stale or manipulated data gracefully.
- **`predictions.py`**: Predictive modeling wrappers.
- **`regime_detector.py`**: Market regime detection (trending, ranging, volatile).
- **`sentiment.py`**: Market sentiment analysis.
- **`simulated_polymarket.py`**: Polymarket prediction simulation.
- **`token_discovery.py`**: Automated discovery of new or trending tokens.
- **`wallet_analyzer.py`**: On-chain wallet tracking and analysis.
- **`whale_watcher.py`**: Large transaction monitoring.

### Risk & Portfolio Management
- **`portfolio.py`**: Portfolio tracking and balancing.
- **`portfolio_risk.py`**: Advanced portfolio-level risk metrics.
- **`risk.py`**: General risk management rules and circuit breakers.
- **`profit_locker.py`**: Logic for locking in profits and trailing stop-losses.
- **`profit_report.py`**: Generation of profit and loss reports.
- **`self_optimizer.py`**: Automated parameter optimization for strategies.

### UI & Alerts
- **`api_server.py`**: FastAPI/Flask server for external integrations.
- **`ascii_charts.py`**: Terminal-based charting utilities.
- **`dashboard.py` / `web_dashboard.py`**: UI components for monitoring.
- **`notifications.py`**: General notification routing.
- **`telegram_alerts.py`**: Telegram-specific alert formatting and delivery.
- **`security.py`**: Security checks and auditing utilities.

### Sub-packages
- **`bridge/`**: Contains `gas_manager.py` and `monitor.py` for cross-chain bridge tracking.
- **`chain/`**: Contains chain-specific logic (e.g., `ethereum.py`, `portfolio_aggregator.py`).
- **`defi/`**: Contains DeFi specific integrations like `liquidity.py`.
- **`ml/`**: Contains machine learning pipelines (`anomaly_detector.py`, `feature_engineer.py`, `model_monitor.py`, `predictor.py`, `regime_predictor.py`, `signal_ensemble.py`).
- **`strategies/`**: Contains specific strategy implementations (`arbitrage.py`, `mean_reversion.py`, `momentum.py`, `smart_dca.py`).
