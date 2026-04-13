# Jupiter Sentinel - 3-Minute Demo Video Script

**Target Audience:** Superteam Earn / Jupiter "Not Your Regular Bounty" Judges
**Goal:** Showcase the autonomous AI agent, the creative "Quotes-as-Oracle" API usage, and the dual (Web + Terminal) dashboards in under 3 minutes.

---

## ⏱️ 0:00 - 0:30 | Introduction & The "Aha!" Moment

**Visual:** Speaker on camera, then transitioning to a screen recording of the project's architecture diagram (from the README or Web Dashboard).

**Talking Points:**
> "Hey Jupiter team! This is Jupiter Sentinel, an autonomous AI DeFi agent built specifically for the 'Not Your Regular Bounty'. Our core innovation is asking: *What if Jupiter's swap quote engine IS the price oracle?* 
>
> Instead of relying on a dedicated price API, Sentinel creatively repurposes the `/swap/v1/quote` endpoint. By quoting a tiny amount like 0.001 SOL every few seconds, we get a real-time, multi-pair price feed that also gives us the exact routing path and price impact. We call this the 'Quotes-as-Oracle' pattern."

---

## ⏱️ 0:30 - 1:00 | Scene 1: The Terminal Dashboard

**Action:** Open a terminal window.

**Command to run:**
```bash
python -m src.dashboard
```

**Visual:** The Rich terminal dashboard launches, displaying real-time market prices, wallet balance, and DEX route intelligence.

**Action to Highlight:** Point out the "Source" column showing `Jupiter /quote`.

**Talking Points:**
> "Let's look at the terminal dashboard. You'll notice the live market prices updating here. This isn't coming from CoinGecko or Birdeye—this is derived entirely from Jupiter Swap Quotes. 
> 
> We also hit the `/program-id-to-label` endpoint, giving the agent a map of over 90 DEXes across Solana. It knows exactly where the liquidity is sitting."

---

## ⏱️ 1:00 - 1:30 | Scene 2: The Web Dashboard

**Action:** Open a second terminal tab and start the web dashboard.

**Command to run:**
```bash
python -m src.web_dashboard
```

**Visual:** Open a web browser to `http://127.0.0.1:8000`. Show the professional dark-themed dashboard, Chart.js performance graph, and active positions.

**Action to Highlight:** Scroll through the active positions and trade history.

**Talking Points:**
> "For a more visual experience, we built a full FastAPI and Chart.js web dashboard. Here, you can track the agent's autonomous performance. It monitors your portfolio, shows active grid bot levels, and tracks the P&L of every dollar-cost averaging (DCA) trade. It’s a complete mission control for your autonomous agent."

---

## ⏱️ 1:30 - 2:15 | Scene 3: The Brain (Route Arbitrage)

**Action:** Stop the web dashboard, clear the terminal. 

**Command to run:**
```bash
python demo.py
```

**Visual:** The demo script outputs the Volatility Scanner, Route Arbitrage Detector, and the Architecture Summary.

**Action to Highlight:** Highlight the "ROUTE ARBITRAGE DETECTOR" section in the output.

**Talking Points:**
> "Now let's see how the brain works. By analyzing the `routePlan` data returned by the quote API, Sentinel discovers that routing depth varies by size. A small trade might take 3 hops for the best price, while a large trade takes a direct route. 
> 
> Sentinel constantly compares these different sizes to find cross-route price discrepancies. It calculates rolling volatility and looks for triangular arbitrage loops like SOL to Token to USDC and back to SOL."

---

## ⏱️ 2:15 - 2:45 | Scene 4: Full Autonomous Execution

**Action:** Clear the terminal. Run the main agent in dry-run/safe mode.

**Command to run:**
```bash
python -m src.main
```

**Visual:** The logs scrolling as the agent scans pairs, calculates trailing stops, and evaluates risk.

**Talking Points:**
> "Finally, here is the agent running fully autonomously. It monitors volatility across 5 pairs 24/7. When an opportunity crosses the threshold, it dynamically sizes the position, executes the swap via the Jupiter V1 API with automatic SOL wrapping, and manages the trade with trailing stops. No human intervention is required."

---

## ⏱️ 2:45 - 3:00 | Outro

**Visual:** Back to speaker on camera or the Web Dashboard overview.

**Talking Points:**
> "Jupiter Sentinel pushes the boundaries of what's possible with the Jupiter APIs by turning the routing engine into an all-in-one oracle, arbitrage detector, and execution layer. 
>
> Oh, and one more thing: this entire project—every line of code—was built autonomously by an AI coding agent. Thanks for watching!"
