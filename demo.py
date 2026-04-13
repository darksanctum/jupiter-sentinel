#!/usr/bin/env python3
"""
Deterministic demo runner for Jupiter Sentinel.

This script patches Jupiter and Solana RPC calls at the urllib boundary so the
existing modules can be exercised without a wallet, API key, or network access.
"""
from __future__ import annotations

import argparse
import io
import json
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from src.arbitrage import RouteArbitrage
from src.config import (
    BONK_MINT,
    JUP_MINT,
    RPC_URL,
    SOL_MINT,
    USDC_MINT,
    WIF_MINT,
)
from src.dex_intel import DexRouteIntel
from src.executor import TradeExecutor
from src.scanner import VolatilityScanner


DEMO_WALLET_LABEL = "demo-wallet-redacted"

ORCA_PID = "Orca111111111111111111111111111111111111111"
RAYDIUM_PID = "Rayd111111111111111111111111111111111111111"
PHOENIX_PID = "Phoe111111111111111111111111111111111111111"
METEORA_PID = "Mete111111111111111111111111111111111111111"
LIFINITY_PID = "Lifi111111111111111111111111111111111111111"


class FakeResponse:
    """Small response shim compatible with urllib callers in this repo."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class DemoMockTransport:
    """Routes quote, label, and RPC requests to deterministic fixtures."""

    def __init__(self) -> None:
        self.stats: Counter[str] = Counter()
        self.oracle_calls: defaultdict[tuple[str, str], int] = defaultdict(int)

        self.dex_labels = {
            ORCA_PID: "Orca",
            RAYDIUM_PID: "Raydium CLMM",
            PHOENIX_PID: "Phoenix",
            METEORA_PID: "Meteora DLMM",
            LIFINITY_PID: "Lifinity",
        }

        # Oracle-like quote sequences used by PriceFeed and VolatilityScanner.
        self.oracle_quotes: dict[tuple[str, str], list[int]] = {
            (SOL_MINT, USDC_MINT): [
                148_200,
                149_600,
                151_100,
                153_400,
                156_900,
                159_800,
                161_500,
                163_400,
                164_200,
                164_800,
                165_100,
                165_400,
            ],
            (JUP_MINT, USDC_MINT): [612_000, 618_000, 624_000, 632_000, 641_000, 648_000],
            (JUP_MINT, SOL_MINT): [4_080_000, 4_140_000, 4_220_000, 4_310_000, 4_390_000, 4_460_000],
            (BONK_MINT, USDC_MINT): [34, 35, 36, 39, 42, 45],
            (WIF_MINT, USDC_MINT): [782_000, 790_000, 804_000, 828_000, 851_000, 878_000],
        }

        # Quotes used by RouteArbitrage, which sends onlyDirectRoutes=false.
        self.arbitrage_quotes: dict[tuple[str, str, int], dict[str, Any]] = {
            (SOL_MINT, USDC_MINT, 100_000): self._quote_payload(
                out_amount=16_100,
                price_impact="0.01",
                labels=["Orca", "Phoenix"],
            ),
            (SOL_MINT, USDC_MINT, 500_000): self._quote_payload(
                out_amount=79_600,
                price_impact="0.02",
                labels=["Meteora DLMM", "Orca"],
            ),
            (SOL_MINT, USDC_MINT, 1_000_000): self._quote_payload(
                out_amount=154_200,
                price_impact="0.05",
                labels=["Raydium CLMM"],
            ),
            (SOL_MINT, USDC_MINT, 5_000_000): self._quote_payload(
                out_amount=742_500,
                price_impact="0.13",
                labels=["Raydium CLMM", "Lifinity"],
            ),
        }

        # Quotes used by TradeExecutor, which also sets asLegacyTransaction=false.
        self.executor_quotes: dict[tuple[str, str, int], dict[str, Any]] = {
            (SOL_MINT, USDC_MINT, 1_000_000): self._quote_payload(
                out_amount=161_400,
                price_impact="0.02",
                labels=["Orca"],
            ),
            (SOL_MINT, USDC_MINT, 25_000_000): self._quote_payload(
                out_amount=4_035_000,
                price_impact="0.07",
                labels=["Meteora DLMM", "Orca"],
            ),
        }

        # Quotes used by DexRouteIntel without the arbitrage/executor flags.
        self.intel_quotes: dict[tuple[str, str, int], dict[str, Any]] = {
            (SOL_MINT, USDC_MINT, 10_000_000): self._route_analysis_payload(
                out_amount=1_615_000,
                price_impact="0.03",
                path=[
                    (ORCA_PID, "amm-orca", 10_000_000, 810_000),
                    (METEORA_PID, "amm-meteora", 810_000, 1_615_000),
                ],
            ),
            (SOL_MINT, USDC_MINT, 50_000_000): self._route_analysis_payload(
                out_amount=8_000_000,
                price_impact="0.08",
                path=[
                    (RAYDIUM_PID, "amm-raydium", 50_000_000, 8_000_000),
                ],
            ),
            (SOL_MINT, USDC_MINT, 100_000_000): self._route_analysis_payload(
                out_amount=15_700_000,
                price_impact="0.14",
                path=[
                    (PHOENIX_PID, "amm-phoenix", 100_000_000, 15_700_000),
                ],
            ),
        }

    def urlopen(self, request: Any, timeout: int = 0) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        parsed = urlparse(url)

        if parsed.path.endswith("/program-id-to-label"):
            self.stats["labels"] += 1
            return FakeResponse(self.dex_labels)

        if parsed.path.endswith("/quote"):
            self.stats["quote"] += 1
            return FakeResponse(self._resolve_quote(parsed))

        if url == RPC_URL:
            return FakeResponse(self._handle_rpc(request))

        raise RuntimeError(f"Unexpected live network call blocked in demo mode: {url}")

    def summary(self) -> dict[str, int]:
        return dict(self.stats)

    def _resolve_quote(self, parsed_url: Any) -> dict[str, Any]:
        params = parse_qs(parsed_url.query)
        input_mint = params["inputMint"][0]
        output_mint = params["outputMint"][0]
        amount = int(params["amount"][0])
        only_direct = params.get("onlyDirectRoutes", [None])[0]
        as_legacy = params.get("asLegacyTransaction", [None])[0]

        if as_legacy == "false":
            fixture = self.executor_quotes.get((input_mint, output_mint, amount))
            if fixture is None:
                raise RuntimeError(
                    f"No executor quote fixture for {input_mint[:6]}->{output_mint[:6]} amount={amount}"
                )
            return fixture

        if only_direct == "false":
            fixture = self.arbitrage_quotes.get((input_mint, output_mint, amount))
            if fixture is None:
                raise RuntimeError(
                    f"No arbitrage quote fixture for {input_mint[:6]}->{output_mint[:6]} amount={amount}"
                )
            return fixture

        intel_fixture = self.intel_quotes.get((input_mint, output_mint, amount))
        if intel_fixture is not None:
            return intel_fixture

        return self._oracle_quote(input_mint, output_mint)

    def _oracle_quote(self, input_mint: str, output_mint: str) -> dict[str, Any]:
        key = (input_mint, output_mint)
        sequence = self.oracle_quotes.get(key)
        if sequence is None:
            raise RuntimeError(f"No oracle quote fixture for {input_mint[:6]}->{output_mint[:6]}")

        index = self.oracle_calls[key]
        self.oracle_calls[key] += 1
        out_amount = sequence[index] if index < len(sequence) else sequence[-1]

        labels = {
            (SOL_MINT, USDC_MINT): ["Orca"],
            (JUP_MINT, USDC_MINT): ["Meteora DLMM"],
            (JUP_MINT, SOL_MINT): ["Phoenix"],
            (BONK_MINT, USDC_MINT): ["Raydium CLMM"],
            (WIF_MINT, USDC_MINT): ["Lifinity"],
        }[key]
        return self._quote_payload(out_amount=out_amount, price_impact="0.02", labels=labels)

    def _handle_rpc(self, request: Any) -> dict[str, Any]:
        payload = json.loads(request.data.decode("utf-8"))
        method = payload.get("method")
        self.stats[f"rpc.{method}"] += 1

        if method == "getBalance":
            return {"jsonrpc": "2.0", "id": payload.get("id", 1), "result": {"value": 3_876_543_210}}

        raise RuntimeError(f"Unexpected RPC method blocked in demo mode: {method}")

    @staticmethod
    def _quote_payload(out_amount: int, price_impact: str, labels: list[str]) -> dict[str, Any]:
        return {
            "outAmount": str(out_amount),
            "priceImpactPct": price_impact,
            "routePlan": [{"swapInfo": {"label": label}} for label in labels],
        }

    @staticmethod
    def _route_analysis_payload(
        out_amount: int,
        price_impact: str,
        path: list[tuple[str, str, int, int]],
    ) -> dict[str, Any]:
        return {
            "outAmount": str(out_amount),
            "priceImpactPct": price_impact,
            "routePlan": [
                {
                    "swapInfo": {
                        "programId": program_id,
                        "ammKey": amm_key,
                        "inAmount": str(in_amount),
                        "outAmount": str(step_out_amount),
                    }
                }
                for program_id, amm_key, in_amount, step_out_amount in path
            ],
        }


def run_demo() -> dict[str, Any]:
    """Execute a safe mock-backed demo and return structured results."""
    transport = DemoMockTransport()

    with patch("urllib.request.urlopen", transport.urlopen):
        executor = TradeExecutor()
        executor.pubkey = DEMO_WALLET_LABEL
        wallet = executor.get_balance()

        scanner = VolatilityScanner()
        alerts: list[dict[str, Any]] = []
        for _ in range(6):
            alerts.extend(scanner.scan_once())

        scanner_rows = [
            {
                "pair": feed.pair_name,
                "price": feed.current_price,
                "change_pct": feed.price_change_pct * 100,
                "volatility": feed.volatility,
            }
            for feed in scanner.feeds
            if feed.current_price is not None
        ]

        arbitrage = RouteArbitrage()
        opportunities = arbitrage.scan_pair(SOL_MINT, USDC_MINT, "SOL/USDC")
        arbitrage_rows = [
            {
                "pair": item.pair,
                "spread_pct": item.spread_pct,
                "buy_route": item.buy_route,
                "sell_route": item.sell_route,
                "estimated_profit_usd": item.estimated_profit_usd,
            }
            for item in opportunities
        ]

        dex_intel = DexRouteIntel()
        discrepancies = dex_intel.find_route_discrepancies(
            SOL_MINT,
            USDC_MINT,
            amounts=[10_000_000, 50_000_000, 100_000_000],
        )

        # The executor logs dry-run details to stdout; keep demo output structured.
        with redirect_stdout(io.StringIO()):
            trade_preview = executor.execute_swap(
                SOL_MINT,
                USDC_MINT,
                25_000_000,
                slippage_bps=75,
                dry_run=True,
            )

    return {
        "mode": "demo",
        "wallet": wallet,
        "scanner": {
            "pairs": scanner_rows,
            "alerts": alerts,
        },
        "arbitrage": arbitrage_rows,
        "dex_intel": {
            "known_dexes": len(dex_intel.dex_labels),
            "discrepancies": discrepancies,
        },
        "trade_preview": trade_preview,
        "mock_api_counts": transport.summary(),
    }


def render_report(report: dict[str, Any]) -> str:
    """Render the structured demo output as readable plain text."""

    def fmt_price(value: float | None) -> str:
        if value is None:
            return "n/a"
        if value >= 1:
            return f"${value:,.4f}"
        return f"${value:.8f}"

    lines = [
        "",
        "JUPITER SENTINEL - DEMO MODE",
        "=" * 72,
        "All Jupiter and Solana RPC calls in this run were served by local mocks.",
        "",
        "1. Wallet Snapshot",
        "-" * 72,
        f"Address: {report['wallet']['address']}",
        (
            f"Balance: {report['wallet']['sol']:.6f} SOL "
            f"(${report['wallet']['usd_value']:.2f}) at ${report['wallet']['sol_price']:.2f}/SOL"
        ),
        "",
        "2. Volatility Scanner",
        "-" * 72,
    ]

    for row in report["scanner"]["pairs"]:
        lines.append(
            f"{row['pair']:10s} {fmt_price(row['price']):>12s}  "
            f"change {row['change_pct']:+6.2f}%  vol {row['volatility']:.4f}"
        )

    alerts = report["scanner"]["alerts"]
    lines.append("")
    lines.append(f"Alerts generated: {len(alerts)}")
    for alert in alerts[:3]:
        lines.append(
            f"  {alert['pair']} {alert['direction']} {abs(alert['change_pct']):.2f}% "
            f"at {fmt_price(alert['price'])} [{alert['severity']}]"
        )

    lines.extend(
        [
            "",
            "3. Route Arbitrage",
            "-" * 72,
        ]
    )
    for row in report["arbitrage"][:3]:
        lines.append(
            f"{row['pair']}: spread {row['spread_pct']:.2f}% | "
            f"buy via {row['buy_route']} | sell via {row['sell_route']} | "
            f"est. profit ${row['estimated_profit_usd']:.4f}"
        )

    lines.extend(
        [
            "",
            "4. DEX Route Intelligence",
            "-" * 72,
            f"Known DEX labels from mocked Jupiter endpoint: {report['dex_intel']['known_dexes']}",
        ]
    )
    for item in report["dex_intel"]["discrepancies"]:
        sizes = ", ".join(item["sizes"])
        lines.append(
            f"{item['route']}: sizes [{sizes}] | avg impact {item['avg_price_impact']:.4f}%"
        )

    trade_preview = report["trade_preview"]
    route_labels = " -> ".join(
        step.get("swapInfo", {}).get("label", "?")
        for step in trade_preview.get("route_plan", [])
    )
    lines.extend(
        [
            "",
            "5. Dry-Run Trade Preview",
            "-" * 72,
            (
                f"Swap status: {trade_preview['status']} | out amount: "
                f"{trade_preview['out_amount'] / 1e6:.6f} USDC | "
                f"impact {trade_preview['price_impact']:.2f}%"
            ),
            f"Route: {route_labels}",
            "",
            "6. Mock API Usage",
            "-" * 72,
        ]
    )

    for key, value in sorted(report["mock_api_counts"].items()):
        lines.append(f"{key}: {value}")

    lines.extend(
        [
            "",
            "No wallet keys, API keys, or live network access were used.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Jupiter Sentinel in deterministic demo mode.")
    parser.add_argument("--json", action="store_true", help="Emit the demo report as JSON.")
    args = parser.parse_args()

    report = run_demo()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
