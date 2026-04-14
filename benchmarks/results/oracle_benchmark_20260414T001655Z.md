# Jupiter Oracle Benchmark Report

- Generated: 2026-04-14T00:16:55+00:00
- Connectivity status: attempted_without_success
- Published Jupiter bucket: 30 requests/minute
- API key present: no
- Oracle quote: `https://api.jup.ag/swap/v1/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=1000000&slippageBps=50&swapMode=ExactIn&restrictIntermediateTokens=true`

## Summary

| Metric | Latency Phase | Throughput Phase | Rate-Limit Probe |
| --- | ---: | ---: | ---: |
| Requests attempted | 6 | 2 | 8 |
| Successes | 0 | 0 | 0 |
| 429 responses | 0 | 0 | 0 |
| Failures | 6 | 2 | 8 |
| Avg response time | n/a | n/a | n/a |
| p95 response time | n/a | n/a | n/a |
| Throughput | 0.00 qps | 0.00 qps | 0.00 qps |

## Rate-Limit Behavior

- First 429 at request: n/a
- First 429 after: n/a seconds
- Max Retry-After observed: n/a seconds

## Realistic Monitoring Capacity

- Capacity basis: `published_bucket_fallback`
- Observed sustained throughput: n/a requests/minute
- Sustainable bucket used: 30 requests/minute
- Safe bucket after 85% utilization: 25 requests/minute
- Reserved for execution + monitoring + metadata: 22 requests/minute
- Remaining scan budget: 3 requests/minute

| Monitoring Interval | Realistic Tokens Monitored |
| --- | ---: |
| 5 sec | 0 |
| 10 sec | 0 |
| 30 sec | 1 |
| 1 min | 3 |
| 5 min | 15 |

Assumption: 1 Jupiter quote per token per cycle against a common quote asset. Multi-size depth scans or extra metadata calls reduce the usable token count.

## Notes

- No successful live Jupiter quotes completed in this environment. Monitoring capacity is modeled from the published bucket, not measured end-to-end network performance.
- The rate-limit probe did not observe a 429 before reaching its request cap.
- Throughput phase errors: ConnectError: [Errno 8] nodename nor servname provided, or not known
