# Jupiter Developer Platform Feedback

## What We Built
Jupiter Sentinel — an autonomous AI DeFi agent that combines 5 Jupiter APIs:
1. **Swap V1** as a real-time price oracle (quotes-as-oracle pattern)
2. **Swap V1** for trade execution with automatic SOL wrapping
3. **Route Plan data** for cross-route arbitrage detection
4. **Price** (derived from quotes) for volatility tracking
5. **Tokens** metadata for token screening

## What Worked Well

### Swap V1 API is production-grade
- `/swap/v1/quote` is incredibly reliable and fast (<200ms)
- Route plan data with labels (Raydium, Orca, etc.) is very useful for analysis
- `wrapAndUnwrapSol` parameter is a lifesaver — no manual wSOL management
- `dynamicComputeUnitLimit` + `prioritizationFeeLamports: "auto"` handles gas perfectly
- No authentication needed for V1 — perfect for prototyping

### Token List API
- Strict token list is comprehensive and well-maintained
- Token search endpoint works great for finding mints

## What Could Be Better

### Swap V2 Requires Portal Sign-in
- The new Swap V2 (`/order` + `/execute`) requires an API key from developers.jup.ag
- Portal requires Google/GitHub OAuth — no programmatic API key generation
- This means AI agents can't self-onboard to V2
- **Suggestion**: Add a "generate API key via CLI" flow for agents

### No WebSocket/Streaming Prices
- Currently polling `/quote` every 30 seconds for price data
- Would love a WebSocket price feed: `wss://api.jup.ag/price/v2/stream`
- This would reduce API load and improve latency for all agents

### Rate Limiting Without Key
- Hit 429 errors when scanning multiple pairs quickly
- With 5 pairs × 4 amounts for arbitrage = 20 requests in rapid succession = rate limited
- **Suggestion**: Higher rate limits for read-only endpoints (quotes, prices)

### Trigger API Documentation is Portal-Gated
- Can't access Trigger API docs without signing in
- Would love public docs at least for the API reference

### No Paper Trading Mode
- Would be great to have a `simulate=true` parameter on `/swap`
- This would let us test strategies without spending SOL
- Currently our "dry run" mode just skips the execution step

## What Surprised Us (In a Good Way)

1. **Quotes work as a price oracle** — we repurposed the swap quote engine as a multi-pair real-time price feed, and it works beautifully. The price precision is excellent.

2. **Route plan reveals DEX labels** — being able to see which DEX each leg routes through (Raydium, Orca, Phoenix, etc.) enabled our cross-route arbitrage detector.

3. **Transaction assembly is seamless** — getting a fully assembled, base58-encoded `VersionedTransaction` back from `/swap` means we just sign and send. No instruction assembly needed.

## What We'd Love to See

1. **Agent-friendly onboarding**: CLI-based API key generation, no OAuth required
2. **WebSocket price feeds**: Real-time streaming for agents
3. **Paper trading mode**: `simulate=true` on swap endpoints
4. **Public Trigger/Lend API docs**: At least the API reference
5. **Batch quote endpoint**: Quote multiple pairs in one request to reduce round trips
6. **Historical price data**: Even last 24h would be useful for volatility calculations

## Overall

Jupiter's APIs are the most developer-friendly DeFi APIs we've used on Solana. The swap engine is rock-solid. The main gap is the developer portal friction for AI agents — we need programmatic onboarding, not OAuth flows.

The fact that we could build a working autonomous trading agent in a few hours with zero documentation issues (V1) speaks to the quality of the API design.
