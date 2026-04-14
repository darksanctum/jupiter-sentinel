# Testing Jupiter Sentinel

This document outlines our testing philosophy, provides a test coverage summary, explains how to run tests, and details what each test suite covers.

## Testing Philosophy

At Jupiter Sentinel, we believe that robust testing is critical for a trading system where financial assets and real-time execution are involved. Our philosophy is rooted in:

1. **High Test Coverage**: We aim for comprehensive test coverage across all critical execution paths, strategies, and risk management components.
2. **Unit Testing first**: Every module must have corresponding unit tests focusing on isolated behavior. We heavily utilize `pytest` monkeypatching to mock external network requests, specifically interactions with the Jupiter API and blockchain RPCs.
3. **Integration Testing**: We ensure that individual components like the `AutoTrader` interact correctly with executors, strategies, and state managers.
4. **Resilience & Error Handling**: Tests must simulate network failures, invalid JSON responses, and RPC timeouts to ensure the system fails gracefully and preserves capital.

## How to Run Tests

We use `pytest` as our primary testing framework.

To run all tests in the project:
```bash
pytest tests/
```

To run a specific test file:
```bash
pytest tests/test_oracle.py
```

To run tests with specific keywords (e.g., all integration tests):
```bash
pytest -k "integration" tests/
```

*(Note: Ensure you have installed the requirements using `pip install -r requirements.txt` before running tests)*

## Test Suite Coverage

The `tests/` directory contains various suites mapping to the core functionalities of the project.

- **`test_analytics.py`**: Verifies the trading performance metrics and historical analytics.
- **`test_anomaly_detector.py`**: Tests the ML anomaly detection logic to spot irregularities in price or volume.
- **`test_autotrader.py` & `test_autotrader_integration.py`**: Tests the core automated trading bot engine and its integration with surrounding components.
- **`test_backtest.py`**: Ensures the backtesting engine accurately simulates historical trades without lookahead bias.
- **`test_benchmark_*.py`**: Verifies performance and timing constraints of the oracle and trading execution systems.
- **`test_bridge_monitor.py` & `test_gas_manager.py`**: Tests cross-chain bridging tracking and L1/L2 gas fee calculations.
- **`test_chain_ethereum.py`**: Validates interactions with Ethereum RPCs and block parsing.
- **`test_correlation_tracker.py`**: Tests the correlation engine which identifies relationships between multiple token pairs.
- **`test_cross_chain_arb*.py`**: Tests the cross-chain arbitrage logic for discrepancies between Solana and EVM chains.
- **`test_demo.py`**: Ensures the demonstrative examples and simulations run without error.
- **`test_executor.py`**: Validates the trade execution engine (Jupiter Swap API routing, slippage checks, and retries).
- **`test_gridbot.py`**: Tests grid trading bot logic and boundary conditions.
- **`test_liquidity.py`**: Verifies DeFi liquidity calculations and pool depth tracking.
- **`test_oracle.py`**: Tests on-chain price feeds, stale data rejection, and fallback oracle sources.
- **`test_portfolio_risk.py` & `test_risk_management.py` / `test_risk_manager.py`**: Ensures all risk checks (max drawdown, exposure limits) are functioning to prevent severe losses.
- **`test_predictor.py` & `test_regime_predictor.py`**: Tests the ML market prediction and regime detection models.
- **`test_profit_locker.py`**: Validates the logic that secures profits automatically after reaching thresholds.
- **`test_rate_limiter.py`**: Tests the token bucket implementations limiting requests to external APIs (like Jupiter).
- **`test_resilience.py`**: Ensures the system can recover from crashes and API disruptions.
- **`test_security.py`**: Checks validation logic for API keys, inputs, and environment security.
- **`test_state_manager.py`**: Tests state persistence and recovery for uninterrupted long-running bots.
- **`test_strategies.py`**: Base strategy interface tests.
- **`test_strategy_*.py` / `test_mean_reversion.py` / `test_momentum.py` / `test_smart_dca.py`**: Validates specific strategy implementations and entry/exit signal generation.
- **`test_token_discovery.py`**: Tests logic for identifying newly minted or rapidly moving tokens.
- **`test_wallet_analyzer.py`**: Verifies wallet balance parsing and tracking.
- **`test_whale_watcher.py`**: Tests on-chain tracking of large trades and wallet movements.

## Coverage Matrix

Below is a matrix indicating the current state of unit test coverage for the modules in `src/`.

### 🟢 Fully / Partially Covered Modules

| Module | Corresponding Test Suite |
|---|---|
| `src/analytics.py` | `tests/test_analytics.py` |
| `src/arbitrage.py` | `tests/test_strategy_arbitrage.py` |
| `src/autotrader.py` | `tests/test_autotrader.py`, `tests/test_autotrader_integration.py` |
| `src/backtest.py` | `tests/test_backtest.py` |
| `src/correlation_tracker.py` | `tests/test_correlation_tracker.py` |
| `src/cross_chain_arb.py` | `tests/test_cross_chain_arb.py` |
| `src/cross_chain_arbitrage.py` | `tests/test_cross_chain_arbitrage.py` |
| `src/executor.py` | `tests/test_executor.py` |
| `src/gridbot.py` | `tests/test_gridbot.py` |
| `src/oracle.py` | `tests/test_oracle.py` |
| `src/portfolio_risk.py` | `tests/test_portfolio_risk.py` |
| `src/predictions.py` | `tests/test_predictor.py` |
| `src/profit_locker.py` | `tests/test_profit_locker.py` |
| `src/rate_limiter.py` | `tests/test_rate_limiter.py` |
| `src/regime_detector.py` | `tests/test_regime_predictor.py` |
| `src/resilience.py` | `tests/test_resilience.py` |
| `src/risk.py` | `tests/test_risk_management.py`, `tests/test_risk_manager.py` |
| `src/security.py` | `tests/test_security.py` |
| `src/state_manager.py` | `tests/test_state_manager.py` |
| `src/token_discovery.py` | `tests/test_token_discovery.py` |
| `src/wallet_analyzer.py` | `tests/test_wallet_analyzer.py` |
| `src/whale_watcher.py` | `tests/test_whale_watcher.py` |
| `src/bridge/gas_manager.py` | `tests/test_gas_manager.py` |
| `src/bridge/monitor.py` | `tests/test_bridge_monitor.py` |
| `src/chain/ethereum.py` | `tests/test_chain_ethereum.py` |
| `src/defi/liquidity.py` | `tests/test_liquidity.py` |
| `src/ml/anomaly_detector.py` | `tests/test_anomaly_detector.py` |
| `src/ml/predictor.py` | `tests/test_predictor.py` |
| `src/ml/regime_predictor.py` | `tests/test_regime_predictor.py` |
| `src/strategies/arbitrage.py` | `tests/test_strategy_arbitrage.py` |
| `src/strategies/mean_reversion.py`| `tests/test_mean_reversion.py` |
| `src/strategies/momentum.py` | `tests/test_momentum.py` |
| `src/strategies/smart_dca.py` | `tests/test_smart_dca.py` |

### 🔴 Uncovered Modules (Needs Tests)

The following modules currently lack explicit unit test coverage and represent areas for improvement in future pull requests:

- `src/api_server.py`
- `src/ascii_charts.py`
- `src/config.py`
- `src/dashboard.py`
- `src/dca.py`
- `src/dex_intel.py`
- `src/jupiter_limits.py`
- `src/live_trader.py`
- `src/main.py`
- `src/microstructure.py`
- `src/monitoring.py`
- `src/multi_timeframe.py`
- `src/notifications.py`
- `src/portfolio.py`
- `src/profit_report.py`
- `src/scanner.py`
- `src/self_optimizer.py`
- `src/sentiment.py`
- `src/simulated_polymarket.py`
- `src/telegram_alerts.py`
- `src/triangular.py`
- `src/validation.py`
- `src/web_dashboard.py`
- `src/chain/portfolio_aggregator.py`
- `src/ml/feature_engineer.py`
- `src/ml/model_monitor.py`
- `src/ml/signal_ensemble.py`
