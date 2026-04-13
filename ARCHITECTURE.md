# 🏛️ Jupiter Sentinel Architecture

Jupiter Sentinel is an autonomous AI DeFi agent built for the Solana ecosystem, specifically engineered to leverage the **Jupiter Aggregator** as its primary source of truth and execution engine. 

This document outlines the core architecture, data flows, and the innovative patterns used to achieve real-time, on-chain intelligence.

---

## 🧩 1. High-Level Module Dependencies

The system is decoupled into independent modules orchestrated by the `JupiterSentinel` main class. This allows for isolated testing and robust error handling.

```mermaid
graph TD
    A[main.py: JupiterSentinel] --> B(oracle.py: PriceFeed)
    A --> C(scanner.py: VolatilityScanner)
    A --> D(executor.py: TradeExecutor)
    A --> E(risk.py: RiskManager)
    A --> F(arbitrage.py: RouteArbitrage)
    A --> G(sentiment.py: SentimentAnalyzer)

    C --> B
    E --> D
    F --> D
    
    style A fill:#1e1e1e,stroke:#00ffcc,stroke-width:2px,color:#fff
    style B fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
    style C fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
    style D fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
    style E fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
    style F fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
    style G fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
```

---

## 🔄 2. Real-Time Data Flow

The Sentinel operates on a continuous, asynchronous loop. Data is ingested from the Jupiter API, processed for volatility and arbitrage opportunities, filtered through risk management, and finally executed.

```mermaid
sequenceDiagram
    participant J as Jupiter API
    participant S as VolatilityScanner
    participant O as RouteArbitrage
    participant R as RiskManager
    participant AI as Sentiment & AI
    participant E as TradeExecutor

    loop Continuous Scan
        S->>J: Fetch Quotes (Oracle)
        J-->>S: Return Swap Data
        S->>S: Calculate Volatility & Momentum
        
        alt High Volatility Alert
            S->>O: Trigger Arbitrage Check
            O->>J: Scan Routes
            J-->>O: Return Spread
            
            S->>R: Request Risk Clearance
            R-->>S: Approve/Deny
            
            S->>AI: Analyze Market Sentiment
            AI-->>S: Trade Decision (Contrarian/Trend)
            
            alt Decision == Execute
                S->>E: Dispatch Trade
                E->>J: Execute Swap
                J-->>E: Tx Confirmation
            end
        end
    end
```

---

## 🔮 3. The "Quotes-as-Oracle" Pattern

Instead of relying on delayed or rate-limited external price oracles, Jupiter Sentinel introduces the **Quotes-as-Oracle** pattern. 

By querying the `/quote` endpoint with a standardized micro-amount, the Sentinel derives the true, deep-liquidity market price in real-time directly from the swap engine.

```mermaid
graph LR
    subgraph Traditional Architecture
        O1[External Oracle] -.-> |Delayed/Batched| SC[Smart Contract]
    end

    subgraph Sentinel Architecture (Quotes-as-Oracle)
        Q[Jupiter /quote API] --> |Real-time, Exact Route| PF[PriceFeed Module]
        PF --> |Standardized Base Amount| V[Volatility Engine]
        PF --> |Derive USD/SOL Price| V
    end

    style Q fill:#00ffcc,stroke:#000,color:#000,font-weight:bold
    style PF fill:#2b2b2b,stroke:#00ffcc,stroke-width:2px,color:#fff
    style V fill:#2b2b2b,stroke:#00ffcc,stroke-width:1px,color:#fff
```

**Benefits of this pattern:**
- **Zero Lag:** Price reflects the exact moment of execution.
- **Liquidity-Aware:** The price implicitly accounts for AMM liquidity and slippage.
- **Self-Contained:** Reduces dependency on third-party infrastructure.

---

## ⚡ 4. Jupiter API Integration Layer

The Sentinel strictly uses the Jupiter ecosystem for both data and execution, ensuring perfect synchronization between what the scanner "sees" and what the executor "does".

```mermaid
graph TD
    subgraph Jupiter Sentinel
        O[oracle.py]
        A[arbitrage.py]
        E[executor.py]
    end

    subgraph Jupiter API Ecosystem
        Q[Quote API / V6]
        S[Swap API]
    end

    O -->|Fetch prices via quotes| Q
    A -->|Find best routes| Q
    E -->|Execute transaction| S
    
    classDef jupiter fill:#1e1e1e,stroke:#2bffaa,stroke-width:2px,color:#fff
    class Q,S jupiter
```

---

## 🛡️ Risk Management & Failsafes

1. **Dry-Run Default:** Boots in dry-run mode unless explicitly passed `--live`.
2. **Slippage Bounds:** Hardcoded limits (e.g., 50 bps) to prevent sandwich attacks.
3. **Contrarian Logic:** Avoids buying during extreme fear events unless validated by strong arbitrage metrics.
