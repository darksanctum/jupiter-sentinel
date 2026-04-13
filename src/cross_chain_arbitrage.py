"""
Cross-chain arbitrage detection for tokens quoted in USDC.

The detector compares the same token across Solana and EVM venues
(Polygon/Ethereum), calculates the fee-adjusted edge, and only surfaces
opportunities whose spread clears bridge, gas, and trading costs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

SOLANA_CHAIN = "solana"
POLYGON_CHAIN = "polygon"
ETHEREUM_CHAIN = "ethereum"
USDC_SYMBOL = "USDC"

SUPPORTED_CHAINS = frozenset({SOLANA_CHAIN, POLYGON_CHAIN, ETHEREUM_CHAIN})
SUPPORTED_CHAIN_PAIRS = frozenset(
    {
        frozenset({SOLANA_CHAIN, POLYGON_CHAIN}),
        frozenset({SOLANA_CHAIN, ETHEREUM_CHAIN}),
    }
)

_CHAIN_ALIASES = {
    "sol": SOLANA_CHAIN,
    "solana": SOLANA_CHAIN,
    "eth": ETHEREUM_CHAIN,
    "ethereum": ETHEREUM_CHAIN,
    "mainnet": ETHEREUM_CHAIN,
    "matic": POLYGON_CHAIN,
    "polygon": POLYGON_CHAIN,
}


def _normalize_chain(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("chain must be a string")

    normalized = _CHAIN_ALIASES.get(value.strip().lower(), value.strip().lower())
    if normalized not in SUPPORTED_CHAINS:
        raise ValueError(f"unsupported chain: {value}")
    return normalized


def _normalize_symbol(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    normalized = value.strip().upper()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


def _coerce_non_negative_float(value: Any, field_name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc

    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be a finite number >= 0")
    return normalized


def _coerce_positive_float(value: Any, field_name: str) -> float:
    normalized = _coerce_non_negative_float(value, field_name)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return normalized


@dataclass(frozen=True)
class CrossChainPriceQuote:
    chain: str
    token_symbol: str
    price_usdc: float
    quote_symbol: str = USDC_SYMBOL
    venue: str = ""
    token_address: Optional[str] = None
    observed_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain", _normalize_chain(self.chain))
        object.__setattr__(
            self, "token_symbol", _normalize_symbol(self.token_symbol, "token_symbol")
        )
        object.__setattr__(
            self, "quote_symbol", _normalize_symbol(self.quote_symbol, "quote_symbol")
        )
        object.__setattr__(
            self, "price_usdc", _coerce_positive_float(self.price_usdc, "price_usdc")
        )
        object.__setattr__(self, "venue", str(self.venue or "").strip())
        if self.token_address is not None:
            object.__setattr__(self, "token_address", str(self.token_address).strip())
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", str(self.observed_at).strip())

    def as_dict(self) -> dict[str, Any]:
        """Serialize the quote."""
        return {
            "chain": self.chain,
            "token_symbol": self.token_symbol,
            "quote_symbol": self.quote_symbol,
            "price_usdc": self.price_usdc,
            "venue": self.venue or None,
            "token_address": self.token_address or None,
            "observed_at": self.observed_at or None,
        }


@dataclass(frozen=True)
class CrossChainFeeSchedule:
    bridge_cost_usdc: float = 0.0
    bridge_fee_bps: float = 0.0
    gas_cost_usdc: float = 0.0
    source_swap_fee_bps: float = 0.0
    destination_swap_fee_bps: float = 0.0
    source_slippage_bps: float = 0.0
    destination_slippage_bps: float = 0.0
    other_fees_usdc: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "bridge_cost_usdc",
            "bridge_fee_bps",
            "gas_cost_usdc",
            "source_swap_fee_bps",
            "destination_swap_fee_bps",
            "source_slippage_bps",
            "destination_slippage_bps",
            "other_fees_usdc",
        ):
            object.__setattr__(
                self,
                field_name,
                _coerce_non_negative_float(getattr(self, field_name), field_name),
            )

    @property
    def source_variable_rate(self) -> float:
        """Return source-side percentage costs as a decimal."""
        return (
            self.source_swap_fee_bps
            + self.source_slippage_bps
            + self.bridge_fee_bps
        ) / 10_000

    @property
    def destination_variable_rate(self) -> float:
        """Return destination-side percentage costs as a decimal."""
        return (self.destination_swap_fee_bps + self.destination_slippage_bps) / 10_000

    @property
    def fixed_costs_usdc(self) -> float:
        """Return fixed bridge, gas, and miscellaneous costs."""
        return self.bridge_cost_usdc + self.gas_cost_usdc + self.other_fees_usdc

    def build_breakdown(
        self,
        *,
        buy_notional_usdc: float,
        sell_notional_usdc: float,
    ) -> "CrossChainFeeBreakdown":
        """Materialize a USD fee breakdown for the trade."""
        source_swap_fee_usdc = buy_notional_usdc * (self.source_swap_fee_bps / 10_000)
        source_slippage_usdc = buy_notional_usdc * (self.source_slippage_bps / 10_000)
        bridge_percentage_fee_usdc = buy_notional_usdc * (self.bridge_fee_bps / 10_000)
        destination_swap_fee_usdc = sell_notional_usdc * (
            self.destination_swap_fee_bps / 10_000
        )
        destination_slippage_usdc = sell_notional_usdc * (
            self.destination_slippage_bps / 10_000
        )

        return CrossChainFeeBreakdown(
            bridge_cost_usdc=self.bridge_cost_usdc,
            bridge_percentage_fee_usdc=bridge_percentage_fee_usdc,
            gas_cost_usdc=self.gas_cost_usdc,
            source_swap_fee_usdc=source_swap_fee_usdc,
            destination_swap_fee_usdc=destination_swap_fee_usdc,
            source_slippage_usdc=source_slippage_usdc,
            destination_slippage_usdc=destination_slippage_usdc,
            other_fees_usdc=self.other_fees_usdc,
        )


@dataclass(frozen=True)
class CrossChainFeeBreakdown:
    bridge_cost_usdc: float
    bridge_percentage_fee_usdc: float
    gas_cost_usdc: float
    source_swap_fee_usdc: float
    destination_swap_fee_usdc: float
    source_slippage_usdc: float
    destination_slippage_usdc: float
    other_fees_usdc: float = 0.0

    @property
    def total_usdc(self) -> float:
        """Return total fees in USDC."""
        return (
            self.bridge_cost_usdc
            + self.bridge_percentage_fee_usdc
            + self.gas_cost_usdc
            + self.source_swap_fee_usdc
            + self.destination_swap_fee_usdc
            + self.source_slippage_usdc
            + self.destination_slippage_usdc
            + self.other_fees_usdc
        )

    def as_dict(self) -> dict[str, float]:
        """Serialize the fee breakdown."""
        return {
            "bridge_cost_usdc": self.bridge_cost_usdc,
            "bridge_percentage_fee_usdc": self.bridge_percentage_fee_usdc,
            "gas_cost_usdc": self.gas_cost_usdc,
            "source_swap_fee_usdc": self.source_swap_fee_usdc,
            "destination_swap_fee_usdc": self.destination_swap_fee_usdc,
            "source_slippage_usdc": self.source_slippage_usdc,
            "destination_slippage_usdc": self.destination_slippage_usdc,
            "other_fees_usdc": self.other_fees_usdc,
            "total_usdc": self.total_usdc,
        }


@dataclass(frozen=True)
class CrossChainProfitability:
    token_symbol: str
    quote_symbol: str
    buy_quote: CrossChainPriceQuote
    sell_quote: CrossChainPriceQuote
    trade_size_tokens: float
    buy_notional_usdc: float
    sell_notional_usdc: float
    gross_spread_usdc: float
    fees: CrossChainFeeBreakdown
    net_profit_usdc: float
    min_profit_usdc: float
    break_even_sell_price_usdc: float

    @property
    def profitable(self) -> bool:
        """Return whether the opportunity clears all fees and the min-profit target."""
        return self.net_profit_usdc > self.min_profit_usdc

    @property
    def price_difference_usdc(self) -> float:
        """Return the raw per-token price gap."""
        return self.sell_quote.price_usdc - self.buy_quote.price_usdc

    @property
    def spread_pct(self) -> float:
        """Return the raw spread as a percentage of the buy price."""
        if self.buy_quote.price_usdc <= 0:
            return 0.0
        return (self.price_difference_usdc / self.buy_quote.price_usdc) * 100

    @property
    def break_even_spread_usdc(self) -> float:
        """Return the per-token edge needed to break even after all fees."""
        return self.break_even_sell_price_usdc - self.buy_quote.price_usdc

    def as_dict(self) -> dict[str, Any]:
        """Serialize the profitability calculation."""
        return {
            "token_symbol": self.token_symbol,
            "quote_symbol": self.quote_symbol,
            "trade_size_tokens": self.trade_size_tokens,
            "buy_quote": self.buy_quote.as_dict(),
            "sell_quote": self.sell_quote.as_dict(),
            "buy_notional_usdc": self.buy_notional_usdc,
            "sell_notional_usdc": self.sell_notional_usdc,
            "price_difference_usdc": self.price_difference_usdc,
            "gross_spread_usdc": self.gross_spread_usdc,
            "spread_pct": self.spread_pct,
            "fees": self.fees.as_dict(),
            "net_profit_usdc": self.net_profit_usdc,
            "min_profit_usdc": self.min_profit_usdc,
            "break_even_sell_price_usdc": self.break_even_sell_price_usdc,
            "break_even_spread_usdc": self.break_even_spread_usdc,
            "profitable": self.profitable,
        }


class CrossChainArbitrageDetector:
    """Detect fee-adjusted price dislocations across Solana and EVM chains."""

    def __init__(
        self,
        *,
        min_profit_usdc: float = 0.0,
        quote_symbol: str = USDC_SYMBOL,
        default_fee_schedule: Optional[CrossChainFeeSchedule] = None,
    ) -> None:
        self.min_profit_usdc = _coerce_non_negative_float(
            min_profit_usdc, "min_profit_usdc"
        )
        self.quote_symbol = _normalize_symbol(quote_symbol, "quote_symbol")
        self.default_fee_schedule = default_fee_schedule or CrossChainFeeSchedule()

    def evaluate_pair(
        self,
        left: CrossChainPriceQuote,
        right: CrossChainPriceQuote,
        *,
        trade_size_tokens: Optional[float] = None,
        trade_notional_usdc: Optional[float] = None,
        fee_schedule: Optional[CrossChainFeeSchedule] = None,
        min_profit_usdc: Optional[float] = None,
    ) -> CrossChainProfitability:
        """Evaluate whether a pair of quotes yields a net-profitable arbitrage."""
        self._validate_quotes(left, right)

        buy_quote, sell_quote = (
            (left, right) if left.price_usdc <= right.price_usdc else (right, left)
        )
        resolved_trade_size_tokens = self._resolve_trade_size_tokens(
            buy_quote=buy_quote,
            trade_size_tokens=trade_size_tokens,
            trade_notional_usdc=trade_notional_usdc,
        )

        resolved_fee_schedule = fee_schedule or self.default_fee_schedule
        resolved_min_profit = (
            self.min_profit_usdc
            if min_profit_usdc is None
            else _coerce_non_negative_float(min_profit_usdc, "min_profit_usdc")
        )

        buy_notional_usdc = resolved_trade_size_tokens * buy_quote.price_usdc
        sell_notional_usdc = resolved_trade_size_tokens * sell_quote.price_usdc
        fees = resolved_fee_schedule.build_breakdown(
            buy_notional_usdc=buy_notional_usdc,
            sell_notional_usdc=sell_notional_usdc,
        )
        gross_spread_usdc = sell_notional_usdc - buy_notional_usdc
        net_profit_usdc = gross_spread_usdc - fees.total_usdc

        break_even_sell_price_usdc = self._break_even_sell_price(
            buy_notional_usdc=buy_notional_usdc,
            trade_size_tokens=resolved_trade_size_tokens,
            fee_schedule=resolved_fee_schedule,
            min_profit_usdc=resolved_min_profit,
        )

        return CrossChainProfitability(
            token_symbol=buy_quote.token_symbol,
            quote_symbol=buy_quote.quote_symbol,
            buy_quote=buy_quote,
            sell_quote=sell_quote,
            trade_size_tokens=resolved_trade_size_tokens,
            buy_notional_usdc=buy_notional_usdc,
            sell_notional_usdc=sell_notional_usdc,
            gross_spread_usdc=gross_spread_usdc,
            fees=fees,
            net_profit_usdc=net_profit_usdc,
            min_profit_usdc=resolved_min_profit,
            break_even_sell_price_usdc=break_even_sell_price_usdc,
        )

    def detect(
        self,
        quotes: Iterable[CrossChainPriceQuote],
        *,
        trade_size_tokens: Optional[float] = None,
        trade_notional_usdc: Optional[float] = None,
        fee_schedule: Optional[CrossChainFeeSchedule] = None,
        min_profit_usdc: Optional[float] = None,
        token_symbol: Optional[str] = None,
    ) -> list[CrossChainProfitability]:
        """Return profitable Solana-vs-EVM arbitrage opportunities."""
        normalized_token = (
            _normalize_symbol(token_symbol, "token_symbol")
            if token_symbol is not None
            else None
        )
        selected_quotes = [
            quote
            for quote in quotes
            if quote.quote_symbol == self.quote_symbol
            and (normalized_token is None or quote.token_symbol == normalized_token)
        ]

        opportunities: list[CrossChainProfitability] = []
        for index, left in enumerate(selected_quotes):
            for right in selected_quotes[index + 1 :]:
                if (
                    left.token_symbol != right.token_symbol
                    or frozenset({left.chain, right.chain}) not in SUPPORTED_CHAIN_PAIRS
                ):
                    continue

                evaluation = self.evaluate_pair(
                    left,
                    right,
                    trade_size_tokens=trade_size_tokens,
                    trade_notional_usdc=trade_notional_usdc,
                    fee_schedule=fee_schedule,
                    min_profit_usdc=min_profit_usdc,
                )
                if evaluation.profitable:
                    opportunities.append(evaluation)

        opportunities.sort(key=lambda item: item.net_profit_usdc, reverse=True)
        return opportunities

    def _validate_quotes(
        self,
        left: CrossChainPriceQuote,
        right: CrossChainPriceQuote,
    ) -> None:
        if left.token_symbol != right.token_symbol:
            raise ValueError("quotes must describe the same token")
        if left.quote_symbol != right.quote_symbol:
            raise ValueError("quotes must use the same quote currency")
        if left.quote_symbol != self.quote_symbol:
            raise ValueError(f"quotes must be denominated in {self.quote_symbol}")
        if left.chain == right.chain:
            raise ValueError("quotes must come from different chains")
        if frozenset({left.chain, right.chain}) not in SUPPORTED_CHAIN_PAIRS:
            raise ValueError("only Solana vs Polygon/Ethereum comparisons are supported")

    def _resolve_trade_size_tokens(
        self,
        *,
        buy_quote: CrossChainPriceQuote,
        trade_size_tokens: Optional[float],
        trade_notional_usdc: Optional[float],
    ) -> float:
        if (trade_size_tokens is None) == (trade_notional_usdc is None):
            raise ValueError(
                "provide exactly one of trade_size_tokens or trade_notional_usdc"
            )

        if trade_size_tokens is not None:
            return _coerce_positive_float(trade_size_tokens, "trade_size_tokens")

        resolved_notional = _coerce_positive_float(
            trade_notional_usdc, "trade_notional_usdc"
        )
        return resolved_notional / buy_quote.price_usdc

    def _break_even_sell_price(
        self,
        *,
        buy_notional_usdc: float,
        trade_size_tokens: float,
        fee_schedule: CrossChainFeeSchedule,
        min_profit_usdc: float,
    ) -> float:
        destination_multiplier = 1.0 - fee_schedule.destination_variable_rate
        if destination_multiplier <= 0:
            return math.inf

        required_sell_notional = (
            buy_notional_usdc * (1.0 + fee_schedule.source_variable_rate)
            + fee_schedule.fixed_costs_usdc
            + min_profit_usdc
        ) / destination_multiplier
        return required_sell_notional / trade_size_tokens


def calculate_profitability(
    token_symbol: str,
    solana_price_usdc: float,
    evm_price_usdc: float,
    *,
    evm_chain: str = POLYGON_CHAIN,
    trade_size_tokens: Optional[float] = None,
    trade_notional_usdc: Optional[float] = None,
    bridge_cost_usdc: float = 0.0,
    bridge_fee_bps: float = 0.0,
    gas_cost_usdc: float = 0.0,
    source_swap_fee_bps: float = 0.0,
    destination_swap_fee_bps: float = 0.0,
    source_slippage_bps: float = 0.0,
    destination_slippage_bps: float = 0.0,
    other_fees_usdc: float = 0.0,
    min_profit_usdc: float = 0.0,
) -> CrossChainProfitability:
    """Convenience helper for one Solana-vs-EVM token comparison."""
    detector = CrossChainArbitrageDetector(min_profit_usdc=min_profit_usdc)
    fee_schedule = CrossChainFeeSchedule(
        bridge_cost_usdc=bridge_cost_usdc,
        bridge_fee_bps=bridge_fee_bps,
        gas_cost_usdc=gas_cost_usdc,
        source_swap_fee_bps=source_swap_fee_bps,
        destination_swap_fee_bps=destination_swap_fee_bps,
        source_slippage_bps=source_slippage_bps,
        destination_slippage_bps=destination_slippage_bps,
        other_fees_usdc=other_fees_usdc,
    )
    return detector.evaluate_pair(
        CrossChainPriceQuote(
            chain=SOLANA_CHAIN,
            token_symbol=token_symbol,
            price_usdc=solana_price_usdc,
        ),
        CrossChainPriceQuote(
            chain=evm_chain,
            token_symbol=token_symbol,
            price_usdc=evm_price_usdc,
        ),
        trade_size_tokens=trade_size_tokens,
        trade_notional_usdc=trade_notional_usdc,
        fee_schedule=fee_schedule,
    )


def detect_arbitrage(
    quotes: Iterable[CrossChainPriceQuote],
    *,
    trade_size_tokens: Optional[float] = None,
    trade_notional_usdc: Optional[float] = None,
    fee_schedule: Optional[CrossChainFeeSchedule] = None,
    min_profit_usdc: float = 0.0,
    token_symbol: Optional[str] = None,
) -> list[CrossChainProfitability]:
    """Module-level wrapper that returns profitable opportunities only."""
    detector = CrossChainArbitrageDetector(
        min_profit_usdc=min_profit_usdc,
        default_fee_schedule=fee_schedule,
    )
    return detector.detect(
        quotes,
        trade_size_tokens=trade_size_tokens,
        trade_notional_usdc=trade_notional_usdc,
        token_symbol=token_symbol,
    )


__all__ = [
    "CrossChainArbitrageDetector",
    "CrossChainFeeBreakdown",
    "CrossChainFeeSchedule",
    "CrossChainPriceQuote",
    "CrossChainProfitability",
    "ETHEREUM_CHAIN",
    "POLYGON_CHAIN",
    "SOLANA_CHAIN",
    "SUPPORTED_CHAIN_PAIRS",
    "SUPPORTED_CHAINS",
    "USDC_SYMBOL",
    "calculate_profitability",
    "detect_arbitrage",
]
