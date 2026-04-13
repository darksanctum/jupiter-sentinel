# 🧠 Sentinel ML Pipeline

This document details the Machine Learning architecture powering Jupiter Sentinel. Our approach combines rigorous mathematical modeling with real-time on-chain data to provide predictive edge and sophisticated risk management.

## 📊 Overview

The Sentinel ML pipeline is designed to be lightweight, dependency-free (relying only on NumPy and SciPy for numerical stability), and hyper-optimized for the Solana DeFi ecosystem. It transforms raw price and volume histories into actionable directional predictions that are seamlessly blended with rule-based strategies.

```ascii
+-------------------+       +-----------------------+       +-------------------+
| Raw Price Feed    |       | Feature Engineering   |       | Logistic Model    |
| (On-chain Oracle) | ----> | (RSI, MACD, BB, Vol)  | ----> | (NumPy / SciPy)   |
+-------------------+       +-----------------------+       +-------------------+
                                                                     |
                                                                     v
+-------------------+       +-----------------------+       +-------------------+
| Execution Engine  |       | Signal Ensemble       |       | ML Output         |
| (Jupiter DEX)     | <---- | (Rule-based + ML)     | <---- | (Direction, Conf) |
+-------------------+       +-----------------------+       +-------------------+
```

## 🛠️ Feature Engineering (`src/ml/feature_engineer.py`)

Our feature extraction pipeline is built for speed and mathematical robustness, converting time-series raw data into predictive signals.

Key extracted features include:
- **Momentum Indicators:** 
  - `RSI` (Relative Strength Index) over a standard 14-period window.
  - `MACD` (Moving Average Convergence Divergence) with fast (12), slow (26), and signal (9) periods.
- **Volatility Metrics:**
  - `Bollinger Bands` (20-period, 2.0 std dev) to detect price extremes and mean-reversion zones.
  - Historical volatility lookbacks (10 to 20 periods) for regime detection.
- **Trend Following:**
  - Multiple Simple Moving Averages (`SMA` 5, 10, 20, 50).
- **Volume Dynamics:**
  - `Volume Ratios` over 20 periods to detect accumulation or distribution patterns.

## 🏗️ Model Architecture (`src/ml/predictor.py`)

To ensure maximum execution speed and zero bloated dependencies, Sentinel uses a custom-built **Logistic Regression Model** from scratch. 

### Architecture Specifications
- **Type:** Binary/Multiclass Logistic Regression.
- **Activation:** Numerically stable Sigmoid (`scipy.special.expit` with a NumPy fallback).
- **Regularization:** L2 Ridge Regression (`l2_strength: 1e-3`) to prevent overfitting on noisy crypto data.
- **Thresholding:** Configurable classification threshold (default `0.5`) tuned per asset pair.

```ascii
      [ X1 (RSI) ] \
      [ X2 (MACD)] --( Weights + L2 Penalty )--> [ Sigmoid ] --> P(Bullish | X)
      [ X3 (Vol) ] /
```

## 🏋️ Training Process

The training loop is executed continually on fresh data:
1. **Data Splitting:** Data is partitioned chronologically (default `test_fraction: 0.25`) to prevent lookahead bias.
2. **Optimization:** Gradient descent with early stopping.
   - `Learning Rate`: 0.1
   - `Max Epochs`: 4000
   - `Patience`: 150 epochs (Stops training if validation loss plateaus).
3. **Atomic Persistence:** Trained weights are persisted atomically to disk (`DATA_DIR/models`) to ensure corruption-free model reloads during live trading.

## 📈 Evaluation Metrics

Model health is continuously evaluated to ensure the trading engine isn't acting on stale or degraded intelligence:
- **Accuracy:** The raw percentage of correctly predicted price directions.
- **Log-Loss / Cross-Entropy:** To measure confidence accuracy; penalizing the model for being confidently wrong.
- **Precision/Recall:** Focused specifically on the "Bullish" or "Bearish" classes depending on the dominant market regime.

## 🤝 Integration with Rule-Based Strategies (`src/ml/signal_ensemble.py`)

ML predictions do not trade in a vacuum. Sentinel utilizes a **Signal Ensemble** to combine stochastic ML predictions with deterministic trading rules.

The `SignalEnsemble` calculates a final weighted score from various strategy signals:

| Component        | Default Weight | Description                                      |
|------------------|----------------|--------------------------------------------------|
| **ML Predictor** | `1.5`          | Our logistic regression prediction.              |
| **Momentum**     | `1.2`          | Short-term price velocity and trend strength.    |
| **Mean Reversion**| `1.0`         | Statistical pullbacks from extremes.             |
| **Regime**       | `1.0`          | Macro market state (trending vs. ranging).       |
| **Sentiment**    | `0.8`          | Social and on-chain sentiment signals.           |

```ascii
[ ML Predictor (Conf: 0.8) ] * 1.5 --+
[ Momentum     (Conf: 0.6) ] * 1.2 --|
[ Mean Reversion(Conf: 0.2)] * 1.0 --+---> [ Ensemble Result ]
[ Regime       (Conf: 0.9) ] * 1.0 --|     - Direction (BULLISH/BEARISH)
[ Sentiment    (Conf: 0.5) ] * 0.8 --+     - Combined Score (-1.0 to 1.0)
                                           - Position Size Multiplier
```

### The Output
The Ensemble outputs an `EnsembleResult` containing a `combined_confidence` and a `position_size_multiplier`. Higher confidence directly scales the capital allocation, allowing Sentinel to automatically size up on high-probability setups and scale down during market uncertainty.
