# Jupiter Sentinel Trading Strategies

Welcome to the Jupiter Sentinel Strategy Guide. This document provides an educational and technical overview of the core trading strategies implemented within the Sentinel ecosystem. Whether you are running the bot in simulation or live trading, understanding these mechanics is crucial for risk management and profitability.

---

## 1. Mean Reversion

### The Logic
Mean reversion is built on the statistical concept that extreme price movements are rare and asset prices tend to return to their historical average (the "mean") over time. When an asset's price deviates significantly from its historical baseline (often measured by moving averages or Bollinger Bands), the strategy anticipates a reversal.

### Entry & Exit Conditions
*   **Entry (Long):** The price drops significantly below the lower standard deviation band (e.g., lower Bollinger Band) or the RSI (Relative Strength Index) falls into extreme oversold territory (e.g., < 30).
*   **Entry (Short):** The price spikes above the upper standard deviation band or RSI indicates overbought conditions (e.g., > 70).
*   **Exit:** The price reverts to the mean (e.g., touches the 20-period Simple Moving Average) or hits a predefined profit target.

### Risk Parameters
*   **Deviation Threshold:** How many standard deviations the price must move before triggering a trade (typically 2.0 to 3.0).
*   **Timeframe:** The lookback period for the moving average (e.g., 20, 50, or 100 periods).
*   **Stop-Loss:** A tight stop-loss is required in case the asset is experiencing a fundamental regime change rather than a temporary deviation (e.g., a "falling knife").

### Expected Performance Characteristics
*   **Win Rate:** High (60-70%).
*   **Risk/Reward Ratio:** Low to Medium. Profit per trade is usually small, but the frequency of wins makes it profitable in ranging, sideways markets.
*   **Weakness:** Performs poorly during strong, sustained trending markets where the price establishes a new mean.

---

## 2. Momentum

### The Logic
Momentum trading is the opposite of mean reversion. It assumes that assets in motion tend to stay in motion. The strategy identifies strong directional trends (up or down) and jumps on board, aiming to ride the wave until the trend shows signs of exhaustion.

### Entry & Exit Conditions
*   **Entry:** Triggered by a breakout above a significant resistance level, a moving average crossover (e.g., the 50 SMA crossing above the 200 SMA—a "Golden Cross"), or high volume accompanying a strong price surge. MACD and ADX are often used as confirming indicators.
*   **Exit:** The trend loses steam, indicated by bearish divergence on the RSI, a moving average cross in the opposite direction, or a trailing stop-loss being hit.

### Risk Parameters
*   **Volume Filter:** Minimum trading volume required to validate a breakout.
*   **Trailing Stop Percentage:** Dynamic stop-loss that moves with the price to lock in profits while allowing the trade room to breathe (e.g., 3-5% trailing).
*   **Confirmation Periods:** Number of consecutive candles required to confirm the trend direction before entry.

### Expected Performance Characteristics
*   **Win Rate:** Moderate (40-50%).
*   **Risk/Reward Ratio:** High. A few massive winning trades typically make up for a series of small, manageable losses (whipsaws).
*   **Weakness:** Susceptible to "fakeouts" and choppy, trendless markets where breakouts fail quickly.

---

## 3. Grid Trading

### The Logic
Grid trading is a market-neutral strategy that seeks to profit from the natural volatility of the market. It places a series of buy and sell orders at predetermined price intervals (a "grid") above and below the current price. As the price fluctuates up and down within the grid, the bot systematically buys low and sells high.

### Entry & Exit Conditions
*   **Entry:** The bot places limit buy orders at grid lines below the current price and limit sell orders at grid lines above.
*   **Exit:** When a buy order is filled, the bot immediately places a corresponding sell order at the next grid line up. Profit is realized continuously as the price "pings" back and forth between grid levels.

### Risk Parameters
*   **Grid Range (Upper & Lower Limits):** The absolute price boundaries within which the bot operates.
*   **Grid Count:** The number of levels within the range. More levels mean more frequent, smaller profits; fewer levels mean less frequent, larger profits.
*   **Investment per Grid:** The capital allocated to each specific tier.

### Expected Performance Characteristics
*   **Win Rate:** Extremely High (near 100% on closed pairs, assuming the price stays within the grid).
*   **Risk/Reward Ratio:** Consistent, incremental gains.
*   **Weakness:** Capital inefficiency (funds are locked in resting limit orders) and "impermanent loss" risk if the price aggressively breaks out of the grid range and doesn't return.

---

## 4. Smart DCA (Dollar Cost Averaging)

### The Logic
Traditional DCA involves buying a fixed dollar amount of an asset at regular time intervals, regardless of price. Jupiter Sentinel's *Smart DCA* enhances this by dynamically adjusting the purchase size and timing based on market conditions, aiming to lower the average entry price more effectively during dips.

### Entry & Exit Conditions
*   **Entry:** Instead of buying strictly on a timer, Smart DCA triggers purchases when the price drops by a certain percentage from the last buy or the local high. The size of the purchase can scale up as the price drops lower (Martingale-style scaling).
*   **Exit:** Exits are usually handled in bulk. When the overall position achieves a target average profit percentage (e.g., +5% across all accumulated tiers), the entire bag is sold.

### Risk Parameters
*   **Price Drop Trigger:** The percentage decline required to trigger the next DCA tier (e.g., 2%, 5%, 10%).
*   **Volume Multiplier:** How much larger each subsequent purchase is compared to the last (e.g., 1.5x, 2.0x).
*   **Max Tiers:** The maximum number of safety orders allowed before the bot stops buying to protect capital.

### Expected Performance Characteristics
*   **Win Rate:** High (as long as the asset eventually recovers).
*   **Risk/Reward Ratio:** Asymmetric. Small, consistent profits during normal conditions, but carries significant drawdown risk if an asset enters a terminal death spiral.
*   **Weakness:** Severe exposure to "black swan" events or fundamentally flawed tokens that never recover.

---

## 5. Arbitrage (Cross-chain & Triangular)

### The Logic
Arbitrage exploits price inefficiencies across different markets or asset pairs. It is mathematically the closest thing to a "risk-free" trade, provided execution is fast enough.
*   **Triangular Arbitrage:** Exploiting price differences between three different assets on the same DEX (e.g., USDC -> SOL -> BONK -> USDC).
*   **Cross-chain/Cross-DEX Arbitrage:** Buying an asset cheaper on one exchange (or chain) and simultaneously selling it higher on another.

### Entry & Exit Conditions
*   **Entry:** The bot constantly scans the order books. When the calculated profit of a loop (accounting for all trading fees, slippage, and gas) exceeds a minimum profit threshold, the trade is executed instantly.
*   **Exit:** Execution happens simultaneously (or atomically within a single transaction where possible). There is no "holding" period.

### Risk Parameters
*   **Minimum Profit Spread:** The absolute minimum net profit margin required to trigger the trade (e.g., > 0.5% net of fees).
*   **Slippage Tolerance:** Strict slippage limits are enforced to prevent the theoretical profit from being destroyed by market movement during execution.
*   **Execution Latency/Gas:** In Solana/Jupiter environments, prioritizing transaction speed via optimal compute unit (CU) pricing is critical.

### Expected Performance Characteristics
*   **Win Rate:** Very High (ideally 100% if atomic execution is possible).
*   **Risk/Reward Ratio:** Exceptional, but opportunities are fleeting and fiercely competitive.
*   **Weakness:** Execution risk. If one leg of a non-atomic arbitrage loop fails or slips heavily, the entire trade can result in a loss. Highly sensitive to network congestion.

---

## 6. Sentiment-Based Trading

### The Logic
Markets are driven by human emotion—fear and greed. The sentiment strategy ingests alternative data sources (Twitter/X mentions, Telegram alpha groups, on-chain whale movements, and general news sentiment) to gauge the mood of the market. It buys when the crowd is irrationally fearful and sells when they are exuberantly greedy, or it rides viral hype cycles.

### Entry & Exit Conditions
*   **Entry:** Triggered by sudden spikes in positive social volume, specific keyword mentions by influential accounts, or a sharp drop in the "Fear & Greed" index to extreme fear (contrarian entry).
*   **Exit:** The social volume peaks and begins to decay, or negative sentiment starts to outweigh positive sentiment in the NLP (Natural Language Processing) analysis.

### Risk Parameters
*   **Sentiment Threshold:** The numerical score required from the NLP engine to validate a signal.
*   **Decay Factor:** How quickly an old news event or tweet loses its weighting in the algorithm.
*   **Position Sizing:** Because sentiment is highly volatile and prone to manipulation (e.g., bot-driven Twitter hype), position sizes are strictly capped.

### Expected Performance Characteristics
*   **Win Rate:** Variable (depends entirely on the quality and speed of the data feed).
*   **Risk/Reward Ratio:** High variance. Can capture massive 10x runs on viral memecoins early, but also prone to buying the top of artificial pump-and-dumps.
*   **Weakness:** Highly susceptible to false positives, sarcastic social media posts misread by NLP, and "buy the rumor, sell the news" events.
