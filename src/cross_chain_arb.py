"""
Jupiter Sentinel - Cross-Size Route Arbitrage Detector
Detects price differences between Jupiter routes at different trade sizes.
"""

import logging
from typing import Any
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from .config import HEADERS, JUPITER_SWAP_V1, SCAN_PAIRS
from .resilience import request_json
from .validation import build_jupiter_quote_url


DEFAULT_AMOUNTS = [
    100_000,
    500_000,
    1_000_000,
    5_000_000,
]


@dataclass(frozen=True)
class RouteQuote:
    amount: int
    out_amount: int
    output_per_input: float
    route_labels: tuple[str, ...]
    price_impact_pct: float
    context_slot: Optional[int] = None

    @property
    def route_signature(self) -> str:
        """Function docstring."""
        return " -> ".join(self.route_labels)

    def as_dict(self) -> dict:
        """Function docstring."""
        return {
            "amount": self.amount,
            "out_amount": self.out_amount,
            "output_per_input": self.output_per_input,
            "route": self.route_signature,
            "price_impact_pct": self.price_impact_pct,
            "context_slot": self.context_slot,
        }


@dataclass(frozen=True)
class RouteSpreadOpportunity:
    pair: str
    better_quote: RouteQuote
    worse_quote: RouteQuote
    spread_pct: float
    estimated_extra_output: int

    def as_dict(self) -> dict:
        """Function docstring."""
        return {
            "pair": self.pair,
            "spread_pct": self.spread_pct,
            "estimated_extra_output": self.estimated_extra_output,
            "better_quote": self.better_quote.as_dict(),
            "worse_quote": self.worse_quote.as_dict(),
        }


class CrossChainArbDetector:
    """
    Scan a Jupiter pair across multiple trade sizes and surface route changes
    that materially change the effective output per unit of input.
    """

    def __init__(
        self,
        min_spread_pct: float = 0.5,
        slippage_bps: int = 50,
        quote_timeout: int = 15,
        require_route_change: bool = True,
    ) -> None:
        """Function docstring."""
        self.min_spread_pct = min_spread_pct
        self.slippage_bps = slippage_bps
        self.quote_timeout = quote_timeout
        self.require_route_change = require_route_change
        self.opportunities: list[RouteSpreadOpportunity] = []

    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
    ) -> Optional[RouteQuote]:
        """Fetch and normalize a Jupiter quote for one trade size."""
        pair_name = f"{input_mint}/{output_mint}"
        try:
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                input_mint,
                output_mint,
                amount,
                self.slippage_bps,
                only_direct_routes=False,
                as_legacy_transaction=False,
            )
            req = urllib.request.Request(url, headers=HEADERS)
            payload = request_json(
                req,
                timeout=self.quote_timeout,
                describe=f"Cross-size quote {pair_name}",
            )
            out_amount = int(payload["outAmount"])
        except Exception:
            return None

        if amount <= 0:
            return None

        return RouteQuote(
            amount=amount,
            out_amount=out_amount,
            output_per_input=out_amount / amount,
            route_labels=self._extract_route_labels(payload.get("routePlan", [])),
            price_impact_pct=self._safe_float(payload.get("priceImpactPct", 0.0)),
            context_slot=payload.get("contextSlot"),
        )

    def scan_pair(
        self,
        input_mint: str,
        output_mint: str,
        pair_name: str,
        amounts: Optional[Iterable[int]] = None,
    ) -> list[RouteSpreadOpportunity]:
        """Compare quotes across sizes and return route spread opportunities."""
        quote_amounts = list(amounts or DEFAULT_AMOUNTS)
        quotes = [
            quote
            for amount in quote_amounts
            if (quote := self.get_quote(input_mint, output_mint, amount)) is not None
        ]

        opportunities: list[RouteSpreadOpportunity] = []
        for index, left in enumerate(quotes):
            for right in quotes[index + 1 :]:
                opportunity = self._compare_quotes(pair_name, left, right)
                if opportunity is not None:
                    opportunities.append(opportunity)

        opportunities.sort(key=lambda item: item.spread_pct, reverse=True)
        self.opportunities.extend(opportunities)
        return opportunities

    def scan_all(
        self,
        pairs: Iterable[tuple[str, str, str]] = SCAN_PAIRS,
        amounts: Optional[Iterable[int]] = None,
    ) -> dict:
        """Run the detector across a collection of pairs."""
        pair_list = list(pairs)
        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "pairs_scanned": len(pair_list),
            "opportunities": [],
        }

        for input_mint, output_mint, pair_name in pair_list:
            opportunities = self.scan_pair(
                input_mint=input_mint,
                output_mint=output_mint,
                pair_name=pair_name,
                amounts=amounts,
            )
            report["opportunities"].extend(
                opportunity.as_dict() for opportunity in opportunities
            )

        return report

    def _compare_quotes(
        self,
        pair_name: str,
        left: RouteQuote,
        right: RouteQuote,
    ) -> Optional[RouteSpreadOpportunity]:
        """Function docstring."""
        if left.output_per_input <= 0 or right.output_per_input <= 0:
            return None

        if self.require_route_change and left.route_signature == right.route_signature:
            return None

        low_rate = min(left.output_per_input, right.output_per_input)
        high_rate = max(left.output_per_input, right.output_per_input)
        spread_pct = ((high_rate - low_rate) / low_rate) * 100

        if spread_pct < self.min_spread_pct:
            return None

        better_quote, worse_quote = (
            (left, right)
            if left.output_per_input > right.output_per_input
            else (right, left)
        )
        reference_amount = min(left.amount, right.amount)
        estimated_extra_output = max(
            int(
                reference_amount
                * (better_quote.output_per_input - worse_quote.output_per_input)
            ),
            0,
        )

        return RouteSpreadOpportunity(
            pair=pair_name,
            better_quote=better_quote,
            worse_quote=worse_quote,
            spread_pct=spread_pct,
            estimated_extra_output=estimated_extra_output,
        )

    def _extract_route_labels(self, route_plan: list[dict]) -> tuple[str, ...]:
        """Function docstring."""
        labels: list[str] = []

        for step in route_plan:
            label = step.get("swapInfo", {}).get("label")
            if label and (not labels or labels[-1] != label):
                labels.append(label)

        if not labels:
            labels.append("Unknown")

        return tuple(labels)

    def _safe_float(self, value: object, default: float = 0.0) -> float:
        """Function docstring."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


def run_standalone() -> None:
    """Function docstring."""
    detector = CrossChainArbDetector()
    report = detector.scan_all()

    logging.debug("%s", "Jupiter Sentinel - Cross-Size Route Arbitrage Detector")
    logging.debug("%s", "=" * 60)

    if not report["opportunities"]:
        logging.debug("%s", "No cross-size route spreads detected.")
        return

    for opportunity in report["opportunities"]:
        logging.debug(
            "%s",
            f"{opportunity['pair']}: {opportunity['spread_pct']:.2f}% spread | "
            f"{opportunity['better_quote']['route']} beats "
            f"{opportunity['worse_quote']['route']}",
        )


if __name__ == "__main__":
    run_standalone()
