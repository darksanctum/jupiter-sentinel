# Jupiter API Reference

This document covers every Jupiter endpoint currently used by Jupiter Sentinel, the parameters each call relies on, current Jupiter platform authentication and rate limits, and working examples in `curl` and Python.

Scope:

- Included: every Jupiter endpoint used by this repo.
- Included: current platform-wide auth and rate-limit rules that apply to those endpoints.
- Not included: full coverage of unrelated Jupiter products such as Lend, Trigger, Recurring, Tokens, Price, or Perps, because Jupiter Sentinel does not call them today.

## Current Integration Summary

Jupiter Sentinel uses three Jupiter Metis Swap V1 endpoints:

| Endpoint | Method | Purpose in this repo | Main call sites |
| --- | --- | --- | --- |
| `/swap/v1/quote` | `GET` | Price discovery, route analysis, volatility checks, arbitrage detection, dashboard pricing | `src/oracle.py`, `src/executor.py`, `src/arbitrage.py`, `src/cross_chain_arb.py`, `src/dex_intel.py`, `src/dca.py`, `src/gridbot.py`, `src/triangular.py`, `src/dashboard.py` |
| `/swap/v1/swap` | `POST` | Build an unsigned swap transaction for signing and broadcast | `src/executor.py` |
| `/swap/v1/program-id-to-label` | `GET` | Map route program IDs to DEX labels for route intelligence | `src/dex_intel.py` |

Important platform note:

- Jupiter's current docs mark Metis Swap V1 as legacy and say it has been superseded by Swap V2.
- Jupiter's current docs also require an `x-api-key` header for these endpoints.
- This repo currently sets only `User-Agent` and `Content-Type` in `src/config.py`, so it is effectively using the lower keyless rate bucket.

## Base URLs, Auth, and Rate Limits

### Base URLs

- Production: `https://api.jup.ag`
- Swap V1 base used by this repo: `https://api.jup.ag/swap/v1`
- Preprod base shown in Jupiter docs for V1 quote endpoints: `https://preprod-quote-api.jup.ag/`

### Authentication

As of April 13, 2026, Jupiter's developer docs say:

- One API key works across Jupiter APIs.
- Swap V1 `quote`, `swap`, and `program-id-to-label` all require `x-api-key` in the docs.
- You get the key from the Jupiter Developer Platform: `https://developers.jup.ag/portal`

Recommended header shape:

```http
x-api-key: <your-api-key>
Content-Type: application/json
User-Agent: JupiterSentinel/1.0
```

### Rate Limits

Jupiter's current portal docs say rate limits are enforced with a 60-second sliding window and shared across all endpoints per account.

| Plan | Requests/second | Requests/minute | API key required |
| --- | ---: | ---: | --- |
| Keyless | 0.5 | 30 | No |
| Free | 1 | 60 | Yes |
| Developer | 10 | 600 | Yes |
| Launch | 50 | 3,000 | Yes |
| Pro | 150 | 9,000 | Yes |

Operational implications for this repo:

- A burst of `5 pairs x 4 quote sizes = 20 requests` can consume most of the keyless minute bucket immediately.
- If you exceed your limit, Jupiter returns `429 Too Many Requests`.
- Because the bucket is shared, quote traffic can starve swap or label calls if you burst aggressively.

Practical guidance:

- Add `x-api-key` support to `HEADERS` before running high-frequency scans.
- Spread quote calls evenly instead of bursting them.
- Implement exponential backoff on `429`.
- Cache the `/program-id-to-label` response at startup or on a long TTL.

## Endpoint Details

### 1. `GET /swap/v1/quote`

Purpose:

- Get the best route and expected output for a swap.
- Jupiter Sentinel repurposes this as its real-time oracle and route-intelligence feed.

Base URL:

```text
https://api.jup.ag/swap/v1/quote
```

Required query parameters:

| Parameter | Type | Meaning |
| --- | --- | --- |
| `inputMint` | `string` | Input token mint address |
| `outputMint` | `string` | Output token mint address |
| `amount` | `uint64` | Raw amount before decimals; input amount for `ExactIn`, output amount for `ExactOut` |

Optional query parameters documented by Jupiter:

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `slippageBps` | `uint16` | `50` | Output slippage threshold for `ExactIn`; input slippage threshold for `ExactOut` |
| `swapMode` | `ExactIn \| ExactOut` | `ExactIn` | `ExactOut` is supported only on some AMMs and is generally not recommended |
| `dexes` | `string[]` | none | Comma-separated allowlist of DEX labels |
| `excludeDexes` | `string[]` | none | Comma-separated DEX denylist |
| `restrictIntermediateTokens` | `boolean` | `true` | Reduces exposure to unstable intermediate hops |
| `onlyDirectRoutes` | `boolean` | `false` | Single-hop routing only; may worsen price |
| `asLegacyTransaction` | `boolean` | `false` | Return a quote intended for legacy transactions |
| `platformFeeBps` | `uint16` | none | If set, `feeAccount` must be passed to `/swap` |
| `maxAccounts` | `uint64` | `64` | Useful when composing your own transaction |
| `instructionVersion` | `V1 \| V2` | `V1` | Swap instruction version |
| `dynamicSlippage` | `boolean` | `false` | No longer applicable on quote; legacy flag |
| `forJitoBundle` | `boolean` | `false` | Excludes DEXes incompatible with Jito bundles |

Parameters used by Jupiter Sentinel:

| Parameter | Where used | Why |
| --- | --- | --- |
| `inputMint` / `outputMint` / `amount` | everywhere | Core quote request |
| `slippageBps=10` | `src/dashboard.py`, `src/gridbot.py`, `src/triangular.py` | Tighter monitoring quotes |
| `slippageBps=50` | `src/oracle.py`, `src/arbitrage.py`, `src/dca.py`, `src/dex_intel.py`, `src/cross_chain_arb.py` | Default analysis and route comparison |
| `slippageBps=300` | `src/executor.py` | Execution quote tolerance |
| `onlyDirectRoutes=false` | `src/arbitrage.py`, `src/cross_chain_arb.py`, `src/executor.py` | Allow full routing for better execution / route comparison |
| `asLegacyTransaction=false` | `src/executor.py`, `src/cross_chain_arb.py` | Keep responses aligned with versioned transaction flow |

Response fields Jupiter Sentinel uses:

| Field | Used for |
| --- | --- |
| `outAmount` | Derived price, execution sizing, DCA stats, arbitrage comparisons |
| `otherAmountThreshold` | Useful slippage floor; not currently consumed directly |
| `priceImpactPct` | Liquidity depth and route-quality checks |
| `routePlan[]` | DEX path analysis and route spread detection |
| `routePlan[].swapInfo.label` | Human-readable DEX names |
| `routePlan[].swapInfo.programId` | Mapped back to labels via `/program-id-to-label` |
| `contextSlot` | Used by `src/cross_chain_arb.py` for quote metadata |
| `timeTaken` | Available for latency diagnostics |

Canonical `curl` example:

```bash
curl --request GET \
  --url "https://api.jup.ag/swap/v1/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000&slippageBps=50" \
  --header "x-api-key: $JUP_API_KEY"
```

Python example matching this repo's price-oracle pattern:

```python
import json
import urllib.request

BASE = "https://api.jup.ag/swap/v1"
HEADERS = {
    "x-api-key": "<api-key>",
    "User-Agent": "JupiterSentinel/1.0",
}

url = (
    f"{BASE}/quote?"
    "inputMint=So11111111111111111111111111111111111111112&"
    "outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&"
    "amount=1000000&"
    "slippageBps=50"
)

req = urllib.request.Request(url, headers=HEADERS)
quote = json.loads(urllib.request.urlopen(req, timeout=10).read())

sol_price_usd = int(quote["outAmount"]) / 1e6 / 0.001
route = [leg["swapInfo"]["label"] for leg in quote.get("routePlan", [])]

print(sol_price_usd, route, quote.get("priceImpactPct"))
```

Repo-specific usage patterns:

- Quotes-as-oracle: `src/oracle.py`
- Dashboard spot pricing: `src/dashboard.py`
- Execution preflight quote: `src/executor.py`
- Cross-size route spread detection: `src/arbitrage.py`, `src/cross_chain_arb.py`
- Triangular loops: `src/triangular.py`
- Grid and DCA valuation: `src/gridbot.py`, `src/dca.py`

### 2. `POST /swap/v1/swap`

Purpose:

- Build an unsigned swap transaction from a quote response.
- Jupiter Sentinel uses this only in `src/executor.py`, then signs and broadcasts through Solana RPC.

Base URL:

```text
https://api.jup.ag/swap/v1/swap
```

Required JSON body fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `userPublicKey` | `string` | Wallet public key that will sign / own the swap |
| `quoteResponse` | `object` | The full response returned by `GET /swap/v1/quote` |

Optional body fields documented by Jupiter:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `payer` | `string` | none | Custom fee payer |
| `wrapAndUnwrapSol` | `boolean` | `true` | Auto-wrap native SOL into WSOL and unwrap back out |
| `useSharedAccounts` | `boolean` | dynamic | Lets Jupiter manage intermediate token accounts |
| `feeAccount` | `string` | none | Required if `platformFeeBps` was set on the quote |
| `trackingAccount` | `string` | none | Tracking public key for downstream analytics |
| `prioritizationFeeLamports` | `object \| integer` | `auto` | Priority-fee or Jito tip controls |
| `asLegacyTransaction` | `boolean` | `false` | Build a legacy tx instead of versioned |
| `destinationTokenAccount` | `string` | none | Pre-existing token account to receive output |
| `nativeDestinationAccount` | `string` | none | Native SOL destination when output mint is SOL |
| `dynamicComputeUnitLimit` | `boolean` | `false` | Simulate to estimate compute units; recommended by Jupiter |
| `skipUserAccountsRpcCalls` | `boolean` | `false` | Skip extra account-existence RPC checks |
| `dynamicSlippage` | `boolean` | `false` | Legacy behavior; Jupiter says it is no longer maintained |
| `computeUnitPriceMicroLamports` | `uint64` | none | Manual compute price override |
| `blockhashSlotsToExpiry` | `uint8` | none | Shorten tx validity window |

Fields used by Jupiter Sentinel:

| Field | Value in repo | Why |
| --- | --- | --- |
| `quoteResponse` | exact quote object | Keeps route and amounts consistent with preflight quote |
| `userPublicKey` | executor wallet pubkey | Required signer identity |
| `wrapAndUnwrapSol` | `true` | Avoid manual WSOL lifecycle management |
| `dynamicComputeUnitLimit` | `true` | Better landing probability and fee efficiency |
| `prioritizationFeeLamports` | `"auto"` | Delegate fee estimation to Jupiter |

Response fields used by the repo:

| Field | Used for |
| --- | --- |
| `swapTransaction` | Base64-encoded unsigned transaction returned by Jupiter and then signed locally |
| `lastValidBlockHeight` | Transaction expiry context |
| `prioritizationFeeLamports` | Observability / debugging |

Canonical `curl` example:

```bash
curl --request POST \
  --url "https://api.jup.ag/swap/v1/swap" \
  --header "Content-Type: application/json" \
  --header "x-api-key: $JUP_API_KEY" \
  --data '{
    "userPublicKey": "<wallet-pubkey>",
    "quoteResponse": {
      "inputMint": "So11111111111111111111111111111111111111112",
      "inAmount": "1000000",
      "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
      "outAmount": "123456",
      "otherAmountThreshold": "122839",
      "swapMode": "ExactIn",
      "slippageBps": 50,
      "priceImpactPct": "0.0001",
      "routePlan": []
    },
    "wrapAndUnwrapSol": true,
    "dynamicComputeUnitLimit": true,
    "prioritizationFeeLamports": "auto"
  }'
```

Python example following `src/executor.py`:

```python
import json
import urllib.request

BASE = "https://api.jup.ag/swap/v1"
HEADERS = {
    "x-api-key": "<api-key>",
    "Content-Type": "application/json",
    "User-Agent": "JupiterSentinel/1.0",
}

quote_response = {
    "inputMint": "So11111111111111111111111111111111111111112",
    "inAmount": "1000000",
    "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "outAmount": "123456",
    "otherAmountThreshold": "122839",
    "swapMode": "ExactIn",
    "slippageBps": 50,
    "priceImpactPct": "0.0001",
    "routePlan": [],
}

body = json.dumps({
    "quoteResponse": quote_response,
    "userPublicKey": "<wallet-pubkey>",
    "wrapAndUnwrapSol": True,
    "dynamicComputeUnitLimit": True,
    "prioritizationFeeLamports": "auto",
}).encode()

req = urllib.request.Request(f"{BASE}/swap", data=body, headers=HEADERS)
swap_payload = json.loads(urllib.request.urlopen(req, timeout=15).read())

print(swap_payload["swapTransaction"])
print(swap_payload["lastValidBlockHeight"])
```

Execution flow in this repo:

1. Fetch quote with `GET /swap/v1/quote`.
2. Submit the full quote object to `POST /swap/v1/swap`.
3. Decode the returned transaction.
4. Sign locally with the configured Solana keypair.
5. Broadcast through `sendTransaction` on `RPC_URL`.

### 3. `GET /swap/v1/program-id-to-label`

Purpose:

- Return a map of Jupiter route program IDs to human-readable DEX labels.
- Jupiter Sentinel uses this in `src/dex_intel.py` to label `routePlan` legs and produce route summaries.

Base URL:

```text
https://api.jup.ag/swap/v1/program-id-to-label
```

Request parameters:

- None

Response:

- JSON object where each key is a program ID and each value is a DEX label.

Canonical `curl` example:

```bash
curl --request GET \
  --url "https://api.jup.ag/swap/v1/program-id-to-label" \
  --header "x-api-key: $JUP_API_KEY"
```

Python example:

```python
import json
import urllib.request

req = urllib.request.Request(
    "https://api.jup.ag/swap/v1/program-id-to-label",
    headers={"x-api-key": "<api-key>", "User-Agent": "JupiterSentinel/1.0"},
)
labels = json.loads(urllib.request.urlopen(req, timeout=10).read())

print(labels.get("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"))
```

Recommended usage:

- Load once at startup.
- Cache it aggressively; the mapping changes much less often than quotes.
- Join it with `routePlan[].swapInfo.programId` or `label` when building analytics.

## Route Plan Fields This Repo Relies On

`routePlan[]` is not a separate endpoint, but it is the main source of route intelligence in Jupiter Sentinel.

Important fields:

| Field | Meaning | Used in |
| --- | --- | --- |
| `swapInfo.ammKey` | AMM account identifier | DEX route inspection |
| `swapInfo.programId` | On-chain program ID | `src/dex_intel.py` label mapping |
| `swapInfo.label` | Human-readable DEX / venue label | Arbitrage and dashboard reports |
| `swapInfo.inAmount` | Amount entering that leg | Route normalization |
| `swapInfo.outAmount` | Amount leaving that leg | Route normalization |
| `swapInfo.feeAmount` | Fee charged on that leg | Route cost inspection |
| `swapInfo.feeMint` | Token mint of the fee | Fee interpretation |
| `percent` | Portion of route allocated to this leg | Multi-path route analysis |

## Recommended Repo Changes

These are not implemented yet, but the docs make them hard requirements for production use:

1. Add `JUP_API_KEY` support in `src/config.py` and include `x-api-key` in `HEADERS`.
2. Add retry and backoff for `urllib` calls that hit `429`.
3. Centralize quote requests behind a small client with pacing and caching.
4. Consider migrating from Metis Swap V1 to Swap V2 when execution flow changes are acceptable.
5. Verify `src/executor.py` transaction decoding, because Jupiter's current docs describe `swapTransaction` as base64 while the repo currently decodes it with `base58`.

## Source Links

Official Jupiter docs:

- Get started: https://developers.jup.ag/docs/get-started
- Rate limits: https://developers.jup.ag/docs/portal/rate-limits
- Pricing: https://developers.jup.ag/pricing
- V1 quote: https://developers.jup.ag/docs/api-reference/swap/v1/quote
- V1 swap: https://developers.jup.ag/docs/api-reference/swap/v1/swap
- V1 program-id-to-label: https://developers.jup.ag/docs/api-reference/swap/v1/program-id-to-label

Repo source files:

- `src/config.py`
- `src/oracle.py`
- `src/executor.py`
- `src/arbitrage.py`
- `src/cross_chain_arb.py`
- `src/dex_intel.py`
- `src/dca.py`
- `src/gridbot.py`
- `src/triangular.py`
- `src/dashboard.py`
