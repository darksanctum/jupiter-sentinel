"""
Triangular arbitrage strategy built from sequential Jupiter quotes.
"""

from __future__ import annotations

import logging
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..config import HEADERS, JUPITER_SWAP_V1, JUP_MINT, SOL_MINT, USDC_MINT
from ..resilience import request_json
from ..validation import build_jupiter_quote_url

DEFAULT_MIN_NET_PROFIT_PCT = 0.5
DEFAULT_SLIPPAGE_BPS = 50
DEFAULT_QUOTE_TIMEOUT = 15
DEFAULT_START_AMOUNT = 10_000_000  # 0.01 SOL
DEFAULT_GAS_COST_LAMPORTS_PER_SWAP = 5_000
DEFAULT_GAS_PRICE_PROBE_LAMPORTS = 1_000_000

KNOWN_TOKEN_DECIMALS = {
    SOL_MINT: 9,
    USDC_MINT: 6,
    JUP_MINT: 6,
}

KNOWN_TOKEN_SYMBOLS = {
    SOL_MINT: "SOL",
    USDC_MINT: "USDC",
    JUP_MINT: "JUP",
}

DEFAULT_TRIANGLES = (
    (SOL_MINT, USDC_MINT, JUP_MINT, SOL_MINT),
)


@dataclass(frozen=True)
class TriangleQuote:
    input_mint: str
    output_mint: str
    input_amount: int
    out_amount: int
    input_decimals: int
    output_decimals: int
    route_labels: tuple[str, ...]
    price_impact_pct: float
    platform_fee_amount: int = 0
    platform_fee_mint: str = ""
    platform_fee_decimals: Optional[int] = None
    context_slot: Optional[int] = None

    @property
    def route_signature(self) -> str:
        """Return a readable route label chain."""
        return " -> ".join(self.route_labels)

    @property
    def input_amount_units(self) -> float:
        """Return the human-readable input amount."""
        return self.input_amount / (10**self.input_decimals)

    @property
    def out_amount_units(self) -> float:
        """Return the human-readable output amount."""
        return self.out_amount / (10**self.output_decimals)

    @property
    def platform_fee_units(self) -> float:
        """Return the human-readable platform fee when the mint is known."""
        if self.platform_fee_amount <= 0 or self.platform_fee_decimals is None:
            return 0.0
        return self.platform_fee_amount / (10**self.platform_fee_decimals)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the normalized quote for reporting."""
        return {
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "input_amount": self.input_amount_units,
            "out_amount": self.out_amount_units,
            "route": self.route_signature,
            "price_impact_pct": self.price_impact_pct,
            "platform_fee_amount": self.platform_fee_units,
            "platform_fee_mint": self.platform_fee_mint or None,
            "context_slot": self.context_slot,
        }


@dataclass(frozen=True)
class TriangleEvaluation:
    path: tuple[str, str, str, str]
    path_name: str
    starting_amount: int
    ending_amount: int
    start_mint: str
    start_symbol: str
    start_decimals: int
    gross_profit_amount: int
    jupiter_fee_amount: int
    gas_cost_amount: int
    net_profit_amount: int
    gross_profit_pct: float
    net_profit_pct: float
    min_profit_pct: float
    legs: tuple[TriangleQuote, TriangleQuote, TriangleQuote]

    @property
    def is_opportunity(self) -> bool:
        """Whether the net round-trip exceeds the configured threshold."""
        return self.net_profit_pct > self.min_profit_pct

    @property
    def starting_amount_units(self) -> float:
        """Return the starting amount in whole-token units."""
        return self.starting_amount / (10**self.start_decimals)

    @property
    def ending_amount_units(self) -> float:
        """Return the ending amount in whole-token units."""
        return self.ending_amount / (10**self.start_decimals)

    @property
    def gross_profit_units(self) -> float:
        """Return the gross round-trip profit in whole-token units."""
        return self.gross_profit_amount / (10**self.start_decimals)

    @property
    def jupiter_fee_units(self) -> float:
        """Return explicit Jupiter platform fees in whole-token units."""
        return self.jupiter_fee_amount / (10**self.start_decimals)

    @property
    def gas_cost_units(self) -> float:
        """Return the estimated gas cost in whole-token units."""
        return self.gas_cost_amount / (10**self.start_decimals)

    @property
    def net_profit_units(self) -> float:
        """Return the fee-adjusted profit in whole-token units."""
        return self.net_profit_amount / (10**self.start_decimals)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the evaluation for reports or downstream alerts."""
        return {
            "path": self.path_name,
            "start_mint": self.start_mint,
            "start_symbol": self.start_symbol,
            "start_amount": self.starting_amount_units,
            "ending_amount": self.ending_amount_units,
            "gross_profit": self.gross_profit_units,
            "jupiter_fee": self.jupiter_fee_units,
            "gas_cost": self.gas_cost_units,
            "net_profit": self.net_profit_units,
            "gross_profit_pct": self.gross_profit_pct,
            "net_profit_pct": self.net_profit_pct,
            "threshold_pct": self.min_profit_pct,
            "opportunity": self.is_opportunity,
            "legs": [leg.as_dict() for leg in self.legs],
        }


class TriangularArbitrageScanner:
    """
    Quote each leg of a triangular loop and flag paths that remain profitable
    after explicit Jupiter platform fees and an estimated network gas cost.

    Jupiter's quoted `outAmount` already reflects the routed AMM output, so this
    strategy only subtracts separately reported `platformFee` values when present.
    """

    def __init__(
        self,
        *,
        min_net_profit_pct: float = DEFAULT_MIN_NET_PROFIT_PCT,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        quote_timeout: int = DEFAULT_QUOTE_TIMEOUT,
        default_start_amount: int = DEFAULT_START_AMOUNT,
        gas_cost_lamports_per_swap: int = DEFAULT_GAS_COST_LAMPORTS_PER_SWAP,
        token_decimals: Optional[Mapping[str, int]] = None,
        token_symbols: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Initialize scanner settings and token metadata."""
        if not math.isfinite(min_net_profit_pct) or min_net_profit_pct < 0:
            raise ValueError("min_net_profit_pct must be a finite percentage >= 0")
        if slippage_bps < 0 or slippage_bps > 10_000:
            raise ValueError("slippage_bps must be between 0 and 10_000")
        if quote_timeout <= 0:
            raise ValueError("quote_timeout must be positive")
        if default_start_amount <= 0:
            raise ValueError("default_start_amount must be positive")
        if gas_cost_lamports_per_swap < 0:
            raise ValueError("gas_cost_lamports_per_swap must be >= 0")

        self.min_net_profit_pct = float(min_net_profit_pct)
        self.slippage_bps = int(slippage_bps)
        self.quote_timeout = int(quote_timeout)
        self.default_start_amount = int(default_start_amount)
        self.gas_cost_lamports_per_swap = int(gas_cost_lamports_per_swap)
        self.token_decimals = dict(KNOWN_TOKEN_DECIMALS)
        self.token_symbols = dict(KNOWN_TOKEN_SYMBOLS)
        self.opportunities: list[TriangleEvaluation] = []
        self._gas_quote_cache: dict[str, TriangleQuote] = {}

        if token_decimals is not None:
            for mint, decimals in token_decimals.items():
                normalized = int(decimals)
                if normalized < 0:
                    raise ValueError("token decimals must be >= 0")
                self.token_decimals[str(mint)] = normalized

        if token_symbols is not None:
            for mint, symbol in token_symbols.items():
                self.token_symbols[str(mint)] = str(symbol)

    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
    ) -> Optional[TriangleQuote]:
        """Fetch and normalize one Jupiter quote leg."""
        if amount <= 0:
            return None

        input_decimals = self._get_token_decimals(input_mint)
        output_decimals = self._get_token_decimals(output_mint)

        try:
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                input_mint,
                output_mint,
                amount,
                self.slippage_bps,
                only_direct_routes=False,
            )
            req = urllib.request.Request(url, headers=HEADERS)
            payload = request_json(
                req,
                timeout=self.quote_timeout,
                describe=f"Triangular arbitrage quote {self._pair_name(input_mint, output_mint)}",
            )
            out_amount = int(payload["outAmount"])
        except Exception as exc:
            logging.debug("Triangular quote failed for %s: %s", input_mint, exc)
            return None

        platform_fee = payload.get("platformFee") or {}
        platform_fee_amount = self._safe_int(platform_fee.get("amount", 0))
        # Jupiter often returns only the fee amount on exact-in quotes. When the
        # mint is omitted we assume the fee is charged on the output token.
        platform_fee_mint = (
            str(platform_fee.get("mint") or "").strip()
            if platform_fee_amount > 0
            else ""
        ) or (output_mint if platform_fee_amount > 0 else "")
        platform_fee_decimals = self.token_decimals.get(platform_fee_mint)

        return TriangleQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            input_amount=int(amount),
            out_amount=out_amount,
            input_decimals=input_decimals,
            output_decimals=output_decimals,
            route_labels=self._extract_route_labels(payload.get("routePlan", [])),
            price_impact_pct=self._safe_float(payload.get("priceImpactPct", 0.0)),
            platform_fee_amount=platform_fee_amount,
            platform_fee_mint=platform_fee_mint,
            platform_fee_decimals=platform_fee_decimals,
            context_slot=payload.get("contextSlot"),
        )

    def evaluate_triangle(
        self,
        triangle: Sequence[str],
        *,
        starting_amount: Optional[int] = None,
    ) -> Optional[TriangleEvaluation]:
        """Evaluate one triangular loop and return its full economics."""
        path = self._normalize_triangle(triangle)
        start_mint = path[0]
        start_amount = int(starting_amount or self.default_start_amount)
        start_decimals = self._get_token_decimals(start_mint)

        legs: list[TriangleQuote] = []
        amount = start_amount

        for input_mint, output_mint in zip(path, path[1:]):
            quote = self.get_quote(input_mint, output_mint, amount)
            if quote is None:
                return None
            legs.append(quote)
            amount = quote.out_amount

        gross_profit_amount = amount - start_amount
        jupiter_fee_amount = sum(
            self._platform_fee_in_start_amount(quote, start_amount, start_mint)
            for quote in legs
        )
        gas_cost_amount = self._estimate_gas_cost(start_mint)
        net_profit_amount = gross_profit_amount - jupiter_fee_amount - gas_cost_amount

        gross_profit_pct = self._pct(gross_profit_amount, start_amount)
        net_profit_pct = self._pct(net_profit_amount, start_amount)

        return TriangleEvaluation(
            path=path,
            path_name=self._format_triangle(path),
            starting_amount=start_amount,
            ending_amount=amount,
            start_mint=start_mint,
            start_symbol=self._token_symbol(start_mint),
            start_decimals=start_decimals,
            gross_profit_amount=gross_profit_amount,
            jupiter_fee_amount=jupiter_fee_amount,
            gas_cost_amount=gas_cost_amount,
            net_profit_amount=net_profit_amount,
            gross_profit_pct=gross_profit_pct,
            net_profit_pct=net_profit_pct,
            min_profit_pct=self.min_net_profit_pct,
            legs=(legs[0], legs[1], legs[2]),
        )

    def scan_triangle(
        self,
        triangle: Sequence[str],
        *,
        starting_amount: Optional[int] = None,
    ) -> Optional[TriangleEvaluation]:
        """Return a triangle only when it clears the net-profit threshold."""
        evaluation = self.evaluate_triangle(
            triangle,
            starting_amount=starting_amount,
        )
        if evaluation is None or not evaluation.is_opportunity:
            return None

        self.opportunities.append(evaluation)
        return evaluation

    def scan_triangles(
        self,
        triangles: Iterable[Sequence[str]] = DEFAULT_TRIANGLES,
        *,
        starting_amount: Optional[int] = None,
    ) -> list[TriangleEvaluation]:
        """Evaluate a collection of triangles and return profitable loops."""
        evaluations: list[TriangleEvaluation] = []

        for triangle in triangles:
            if evaluation := self.scan_triangle(
                triangle,
                starting_amount=starting_amount,
            ):
                evaluations.append(evaluation)

        return evaluations

    def scan_all(
        self,
        triangles: Iterable[Sequence[str]] = DEFAULT_TRIANGLES,
        *,
        starting_amount: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return a timestamped report of all profitable triangular loops."""
        triangle_list = [self._normalize_triangle(triangle) for triangle in triangles]
        opportunities = self.scan_triangles(
            triangle_list,
            starting_amount=starting_amount,
        )

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "triangles_scanned": len(triangle_list),
            "opportunities": [evaluation.as_dict() for evaluation in opportunities],
        }

    def _extract_route_labels(self, route_plan: list[dict[str, Any]]) -> tuple[str, ...]:
        """Collapse a Jupiter routePlan into distinct readable labels."""
        labels: list[str] = []

        for step in route_plan:
            label = str((step.get("swapInfo") or {}).get("label") or "").strip()
            if label and (not labels or labels[-1] != label):
                labels.append(label)

        if not labels:
            labels.append("Unknown")

        return tuple(labels)

    def _platform_fee_in_start_amount(
        self,
        quote: TriangleQuote,
        start_amount: int,
        start_mint: str,
    ) -> int:
        """Convert an explicit platform fee into raw units of the starting token."""
        if quote.platform_fee_amount <= 0:
            return 0

        fee_mint = quote.platform_fee_mint or quote.output_mint
        if fee_mint == start_mint:
            return quote.platform_fee_amount
        if fee_mint == quote.input_mint:
            return self._convert_amount_via_reference(
                amount=quote.platform_fee_amount,
                source_mint=quote.input_mint,
                target_mint=start_mint,
                source_reference=quote.input_amount,
                target_reference=start_amount,
            )
        if fee_mint == quote.output_mint:
            return self._convert_amount_via_reference(
                amount=quote.platform_fee_amount,
                source_mint=quote.output_mint,
                target_mint=start_mint,
                source_reference=quote.out_amount,
                target_reference=start_amount,
            )
        return 0

    def _estimate_gas_cost(self, start_mint: str) -> int:
        """Estimate the three-leg gas cost in raw units of the starting token."""
        total_lamports = self.gas_cost_lamports_per_swap * 3
        if total_lamports <= 0:
            return 0
        if start_mint == SOL_MINT:
            return total_lamports

        quote = self._gas_quote_cache.get(start_mint)
        probe_amount = max(total_lamports, DEFAULT_GAS_PRICE_PROBE_LAMPORTS)
        if quote is None or quote.input_amount != probe_amount:
            quote = self.get_quote(SOL_MINT, start_mint, probe_amount)
            if quote is None:
                raise RuntimeError(
                    f"Unable to quote SOL gas cost into {self._token_symbol(start_mint)}"
                )
            self._gas_quote_cache[start_mint] = quote

        return self._convert_amount_via_reference(
            amount=total_lamports,
            source_mint=SOL_MINT,
            target_mint=start_mint,
            source_reference=quote.input_amount,
            target_reference=quote.out_amount,
        )

    def _convert_amount_via_reference(
        self,
        *,
        amount: int,
        source_mint: str,
        target_mint: str,
        source_reference: int,
        target_reference: int,
    ) -> int:
        """Scale an amount across two mints using an observed quote ratio."""
        if amount <= 0 or source_reference <= 0 or target_reference <= 0:
            return 0

        source_units = self._to_units(source_mint, amount)
        source_reference_units = self._to_units(source_mint, source_reference)
        target_reference_units = self._to_units(target_mint, target_reference)
        if source_reference_units <= 0 or target_reference_units <= 0:
            return 0

        converted_units = source_units * (target_reference_units / source_reference_units)
        return self._to_raw(target_mint, converted_units)

    def _normalize_triangle(self, triangle: Sequence[str]) -> tuple[str, str, str, str]:
        """Validate and normalize the triangle path."""
        if len(triangle) != 4:
            raise ValueError("triangle must contain exactly four mints")

        normalized = tuple(str(mint) for mint in triangle)
        if normalized[0] != normalized[-1]:
            raise ValueError("triangle must start and end with the same mint")

        for mint in normalized:
            self._get_token_decimals(mint)

        return normalized  # type: ignore[return-value]

    def _pair_name(self, input_mint: str, output_mint: str) -> str:
        """Return a readable pair label."""
        return f"{self._token_symbol(input_mint)}/{self._token_symbol(output_mint)}"

    def _format_triangle(self, triangle: Sequence[str]) -> str:
        """Return a readable triangle path."""
        return " -> ".join(self._token_symbol(mint) for mint in triangle)

    def _get_token_decimals(self, mint: str) -> int:
        """Return cached token decimals for a known mint."""
        try:
            return self.token_decimals[mint]
        except KeyError as exc:
            raise ValueError(f"Missing decimals for mint {mint}") from exc

    def _token_symbol(self, mint: str) -> str:
        """Return a cached symbol or a shortened mint fallback."""
        symbol = self.token_symbols.get(mint)
        if symbol:
            return symbol
        return mint[:4]

    def _to_units(self, mint: str, amount: int) -> float:
        """Convert raw token units into a normalized float amount."""
        return amount / (10**self._get_token_decimals(mint))

    def _to_raw(self, mint: str, amount_units: float) -> int:
        """Convert a normalized amount back into raw token units."""
        return int(round(amount_units * (10**self._get_token_decimals(mint))))

    def _pct(self, amount: int, reference: int) -> float:
        """Convert a raw profit delta into a percentage of the starting amount."""
        if reference <= 0:
            return 0.0
        return (amount / reference) * 100

    def _safe_float(self, value: object, default: float = 0.0) -> float:
        """Coerce a possibly-missing numeric payload field."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _safe_int(self, value: object, default: int = 0) -> int:
        """Coerce a possibly-missing integer payload field."""
        try:
            if value in (None, ""):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default


def scan_for_opportunities(
    triangles: Iterable[Sequence[str]] = DEFAULT_TRIANGLES,
    *,
    starting_amount: int = DEFAULT_START_AMOUNT,
    min_net_profit_pct: float = DEFAULT_MIN_NET_PROFIT_PCT,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    quote_timeout: int = DEFAULT_QUOTE_TIMEOUT,
    gas_cost_lamports_per_swap: int = DEFAULT_GAS_COST_LAMPORTS_PER_SWAP,
    token_decimals: Optional[Mapping[str, int]] = None,
    token_symbols: Optional[Mapping[str, str]] = None,
) -> list[TriangleEvaluation]:
    """Convenience entrypoint for a single arbitrage scan pass."""
    scanner = TriangularArbitrageScanner(
        min_net_profit_pct=min_net_profit_pct,
        slippage_bps=slippage_bps,
        quote_timeout=quote_timeout,
        default_start_amount=starting_amount,
        gas_cost_lamports_per_swap=gas_cost_lamports_per_swap,
        token_decimals=token_decimals,
        token_symbols=token_symbols,
    )
    return scanner.scan_triangles(triangles, starting_amount=starting_amount)


__all__ = [
    "DEFAULT_GAS_COST_LAMPORTS_PER_SWAP",
    "DEFAULT_MIN_NET_PROFIT_PCT",
    "DEFAULT_START_AMOUNT",
    "DEFAULT_TRIANGLES",
    "TriangleEvaluation",
    "TriangleQuote",
    "TriangularArbitrageScanner",
    "scan_for_opportunities",
]
