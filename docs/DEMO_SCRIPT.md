# 🎬 Jupiter Sentinel: 3-Minute Demo Script

**Target Audience:** Jupiter Bounty Judges
**Estimated Time:** 3 minutes
**Goal:** Showcase the autonomous nature of the agent and the innovative "Quotes-as-Oracle" architecture.

---

## ⏱️ 0:00 - 0:30 | Introduction & The "Aha!" Moment

**Screen:** Show the GitHub README header or a title slide.

**Talking Points:**
> "Hi judges! Welcome to Jupiter Sentinel, an autonomous AI DeFi agent built for the 'Not Your Regular Bounty'.
> We started with a simple question: What if Jupiter's swap quote engine *is* the price oracle?
> Instead of relying on paid or delayed price feeds, Sentinel repurposes Jupiter's `/swap/v1/quote` endpoint as a real-time, zero-cost, multi-pair price oracle. It gets the exact price, route, and impact for the exact liquidity pools we trade on."

---

## ⏱️ 0:30 - 1:15 | Terminal Dashboard & DEX Intelligence

**Command:** `python -m src.dashboard`
**Screen:** Terminal showing the Rich dashboard with live prices and DEX routes.

**Talking Points:**
> "Let's look at the Sentinel's brain. Running the dashboard, you can see real-time market prices derived purely from Jupiter swap quotes.
> We're pulling quotes for tiny amounts like 0.001 SOL every few seconds.
> Notice the DEX Route Intelligence. By hitting the `/program-id-to-label` endpoint, the agent maps over 90 DEXes. Raydium, Orca, Meteora... Sentinel knows them all. This is a treasure map for cross-route arbitrage."

---

## ⏱️ 1:15 - 2:00 | Web Dashboard & Risk Management

**Command:** `python -m src.web_dashboard`
**Screen:** Browser showing `http://127.0.0.1:8000` with the Chart.js portfolio performance and active positions.

**Talking Points:**
> "Because the terminal isn't enough, Sentinel also spins up a FastAPI-powered Web UI. Here we track portfolio performance, active positions, and trade history visually.
> The agent doesn't just scan; it manages risk. It calculates dynamic position sizes based on volatility and uses trailing stops. It never risks more than a set percentage of the balance, ensuring it survives volatile market swings."

---

## ⏱️ 2:00 - 2:40 | The Autonomous Loop & Arbitrage

**Command:** `python -m src.main`
**Screen:** Terminal showing the agent's main orchestrator loop (scanning, finding opportunities, executing) — running in dry-run mode.

**Talking Points:**
> "Here is the autonomous loop in action. The agent runs 24/7 without human intervention.
> It performs Route Depth Analysis: it compares quotes for small amounts versus large amounts. Small sizes might route through 3 hops for the best price, while large sizes take direct routes. Sentinel detects the price discrepancies between these routes to find invisible arbitrage opportunities.
> Once found, it auto-wraps SOL, signs, and broadcasts the transaction using the V1 Swap API."

---

## ⏱️ 2:40 - 3:00 | Conclusion & Wrap Up

**Screen:** Show the "Built By Hermes" section in the README or a concluding slide.

**Talking Points:**
> "To sum it up: Sentinel is a complete, self-healing algorithmic trader that uses Jupiter's own routing engine as its source of truth.
> Best of all? This entire project, from the API integrations to the dashboards, was built by an autonomous AI coding agent in a single session. No human wrote a single line of code.
> Thank you for watching, and we hope you enjoy Jupiter Sentinel!"