"""
Ethereum mainnet helpers for swap economics.

The module intentionally stays lightweight: it supports the token set this
project currently cares about on Ethereum mainnet (USDC, USDT, WETH), can
estimate L1 gas costs for those swaps, and can decide whether a quoted trade
remains profitable after gas.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ..config import HEADERS
from ..resilience import request_json

CHAIN_ID = 1
CHAIN_NAME = "ethereum"
ETH_SYMBOL = "ETH"
DEFAULT_RPC_TIMEOUT = 15
DEFAULT_MIN_PROFIT_USD = 0.0
DEFAULT_APPROVAL_GAS_LIMIT = 45_000
ETH_USD_SPOT_URL = "https://api.coinbase.com/v2/prices/ETH-USD/spot"


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int
    stable: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialize token metadata."""
        return {
            "symbol": self.symbol,
            "address": self.address,
            "decimals": self.decimals,
            "stable": self.stable,
        }


USDC = Token(
    symbol="USDC",
    address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    decimals=6,
    stable=True,
)
USDT = Token(
    symbol="USDT",
    address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
    decimals=6,
    stable=True,
)
WETH = Token(
    symbol="WETH",
    address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    decimals=18,
    stable=False,
)

SUPPORTED_TOKENS: dict[str, Token] = {
    USDC.symbol: USDC,
    USDT.symbol: USDT,
    WETH.symbol: WETH,
}

SUPPORTED_PAIRS = frozenset(
    {
        ("USDC", "USDT"),
        ("USDT", "USDC"),
        ("USDC", "WETH"),
        ("WETH", "USDC"),
        ("USDT", "WETH"),
        ("WETH", "USDT"),
    }
)

DEFAULT_GAS_LIMITS: dict[tuple[str, str], int] = {
    ("USDC", "USDT"): 125_000,
    ("USDT", "USDC"): 125_000,
    ("USDC", "WETH"): 160_000,
    ("WETH", "USDC"): 160_000,
    ("USDT", "WETH"): 160_000,
    ("WETH", "USDT"): 160_000,
}


@dataclass(frozen=True)
class TradeQuote:
    input_token: str
    output_token: str
    amount_in: int
    amount_out: int
    input_decimals: int
    output_decimals: int
    route: str = ""
    price_impact_pct: float = 0.0

    @property
    def input_amount_units(self) -> float:
        """Return the quoted input size in whole-token units."""
        return self.amount_in / (10**self.input_decimals)

    @property
    def output_amount_units(self) -> float:
        """Return the quoted output size in whole-token units."""
        return self.amount_out / (10**self.output_decimals)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the quote."""
        return {
            "input_token": self.input_token,
            "output_token": self.output_token,
            "amount_in": self.input_amount_units,
            "amount_out": self.output_amount_units,
            "route": self.route or None,
            "price_impact_pct": self.price_impact_pct,
        }


@dataclass(frozen=True)
class GasEstimate:
    input_token: str
    output_token: str
    gas_limit: int
    gas_price_wei: int
    gas_cost_wei: int
    gas_cost_eth: float
    gas_cost_usd: Optional[float]
    includes_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialize the gas estimate."""
        return {
            "input_token": self.input_token,
            "output_token": self.output_token,
            "gas_limit": self.gas_limit,
            "gas_price_wei": self.gas_price_wei,
            "gas_cost_wei": self.gas_cost_wei,
            "gas_cost_eth": self.gas_cost_eth,
            "gas_cost_usd": self.gas_cost_usd,
            "includes_approval": self.includes_approval,
        }


@dataclass(frozen=True)
class TradeProfitability:
    quote: TradeQuote
    gas_estimate: GasEstimate
    input_value_usd: float
    output_value_usd: float
    gross_profit_usd: float
    net_profit_usd: float
    min_profit_usd: float
    output_price_usd: float
    break_even_output_amount: int

    @property
    def profitable(self) -> bool:
        """Whether the trade clears the configured net-profit threshold."""
        return self.net_profit_usd > self.min_profit_usd

    @property
    def gross_profit_pct(self) -> float:
        """Gross edge as a percentage of input notional."""
        if self.input_value_usd <= 0:
            return 0.0
        return (self.gross_profit_usd / self.input_value_usd) * 100

    @property
    def net_profit_pct(self) -> float:
        """Net edge after gas as a percentage of input notional."""
        if self.input_value_usd <= 0:
            return 0.0
        return (self.net_profit_usd / self.input_value_usd) * 100

    @property
    def break_even_output_amount_units(self) -> float:
        """Return the quoted output required to break even after gas."""
        return self.break_even_output_amount / (10**self.quote.output_decimals)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the profitability analysis."""
        return {
            "quote": self.quote.as_dict(),
            "gas": self.gas_estimate.as_dict(),
            "input_value_usd": self.input_value_usd,
            "output_value_usd": self.output_value_usd,
            "gross_profit_usd": self.gross_profit_usd,
            "net_profit_usd": self.net_profit_usd,
            "gross_profit_pct": self.gross_profit_pct,
            "net_profit_pct": self.net_profit_pct,
            "min_profit_usd": self.min_profit_usd,
            "break_even_output_amount": self.break_even_output_amount_units,
            "profitable": self.profitable,
        }


class EthereumChain:
    """Ethereum mainnet metadata and trade-economics helper."""

    def __init__(
        self,
        *,
        rpc_url: Optional[str] = None,
        rpc_timeout: int = DEFAULT_RPC_TIMEOUT,
        min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
        gas_limits: Optional[Mapping[tuple[str, str], int]] = None,
        token_prices_usd: Optional[Mapping[str, float]] = None,
        eth_usd_spot_url: str = ETH_USD_SPOT_URL,
    ) -> None:
        if rpc_timeout <= 0:
            raise ValueError("rpc_timeout must be positive")
        if not math.isfinite(min_profit_usd) or min_profit_usd < 0:
            raise ValueError("min_profit_usd must be finite and >= 0")

        self.rpc_url = (rpc_url or os.environ.get("ETHEREUM_RPC_URL", "")).strip() or None
        self.rpc_timeout = int(rpc_timeout)
        self.min_profit_usd = float(min_profit_usd)
        self.eth_usd_spot_url = eth_usd_spot_url

        self.tokens = dict(SUPPORTED_TOKENS)
        self._address_index = {
            token.address.lower(): symbol for symbol, token in self.tokens.items()
        }
        self.gas_limits = dict(DEFAULT_GAS_LIMITS)

        if gas_limits is not None:
            for pair, gas_limit in gas_limits.items():
                input_token, output_token = pair
                self.gas_limits[
                    (
                        self._normalize_token(input_token),
                        self._normalize_token(output_token),
                    )
                ] = self._coerce_positive_int(gas_limit, "gas_limit")

        self.token_prices_usd: dict[str, float] = {}
        if token_prices_usd is not None:
            for token, price in token_prices_usd.items():
                symbol = self._normalize_price_key(token)
                self.token_prices_usd[symbol] = self._coerce_positive_float(
                    price, f"{symbol} price"
                )

    def token(self, token: str) -> Token:
        """Return normalized token metadata."""
        return self.tokens[self._normalize_token(token)]

    def supports_token(self, token: str) -> bool:
        """Return whether the token is part of the supported Ethereum set."""
        try:
            self._normalize_token(token)
        except ValueError:
            return False
        return True

    def supports_pair(self, input_token: str, output_token: str) -> bool:
        """Return whether the project supports this direct trade pair."""
        try:
            pair = (
                self._normalize_token(input_token),
                self._normalize_token(output_token),
            )
        except ValueError:
            return False
        return pair in SUPPORTED_PAIRS

    def to_units(self, token: str, amount: int) -> float:
        """Convert raw token units into a whole-token float amount."""
        metadata = self.token(token)
        return amount / (10**metadata.decimals)

    def to_raw(self, token: str, amount_units: float) -> int:
        """Convert a whole-token amount into raw token units."""
        metadata = self.token(token)
        if not math.isfinite(amount_units) or amount_units < 0:
            raise ValueError("amount_units must be finite and >= 0")
        return int(round(amount_units * (10**metadata.decimals)))

    def build_quote(
        self,
        input_token: str,
        output_token: str,
        amount_in: int | float,
        amount_out: int | float,
        *,
        amounts_are_raw: bool = True,
        route: str = "",
        price_impact_pct: float = 0.0,
    ) -> TradeQuote:
        """Create a normalized quote object for one supported pair."""
        input_symbol = self._normalize_token(input_token)
        output_symbol = self._normalize_token(output_token)
        if not self.supports_pair(input_symbol, output_symbol):
            raise ValueError(
                f"Unsupported Ethereum pair: {input_symbol}/{output_symbol}"
            )

        if amounts_are_raw:
            amount_in_raw = self._coerce_positive_int(amount_in, "amount_in")
            amount_out_raw = self._coerce_positive_int(amount_out, "amount_out")
        else:
            amount_in_raw = self.to_raw(input_symbol, float(amount_in))
            amount_out_raw = self.to_raw(output_symbol, float(amount_out))

        return TradeQuote(
            input_token=input_symbol,
            output_token=output_symbol,
            amount_in=amount_in_raw,
            amount_out=amount_out_raw,
            input_decimals=self.token(input_symbol).decimals,
            output_decimals=self.token(output_symbol).decimals,
            route=route.strip(),
            price_impact_pct=self._coerce_non_negative_float(
                price_impact_pct, "price_impact_pct"
            ),
        )

    def estimate_gas_limit(
        self,
        input_token: str,
        output_token: str,
        *,
        include_approval: bool = False,
    ) -> int:
        """Return a conservative gas limit for a supported swap route."""
        pair = (
            self._normalize_token(input_token),
            self._normalize_token(output_token),
        )
        if pair not in SUPPORTED_PAIRS:
            raise ValueError(f"Unsupported Ethereum pair: {pair[0]}/{pair[1]}")

        gas_limit = self.gas_limits[pair]
        if include_approval:
            gas_limit += DEFAULT_APPROVAL_GAS_LIMIT
        return gas_limit

    def get_gas_price_wei(self) -> int:
        """Fetch the current network gas price via Ethereum JSON-RPC."""
        result = self._rpc("eth_gasPrice", [])
        try:
            return int(str(result), 16)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Unexpected eth_gasPrice result: {result}") from exc

    def get_eth_price_usd(self) -> float:
        """Fetch a spot ETH/USD price for gas-cost conversion."""
        req = urllib.request.Request(
            self.eth_usd_spot_url,
            headers={
                "User-Agent": HEADERS.get("User-Agent", "JupiterSentinel/1.0"),
                "Accept": "application/json",
            },
        )
        payload = request_json(
            req,
            timeout=self.rpc_timeout,
            describe="ETH/USD spot price",
        )
        try:
            return self._coerce_positive_float(
                payload["data"]["amount"],
                "ETH/USD price",
            )
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Unexpected ETH/USD spot payload") from exc

    def estimate_gas_cost(
        self,
        input_token: str,
        output_token: str,
        *,
        gas_price_wei: Optional[int] = None,
        gas_limit: Optional[int] = None,
        eth_price_usd: Optional[float] = None,
        include_approval: bool = False,
    ) -> GasEstimate:
        """Estimate L1 gas in wei, ETH, and USD for a supported trade."""
        input_symbol = self._normalize_token(input_token)
        output_symbol = self._normalize_token(output_token)
        limit = (
            self._coerce_positive_int(gas_limit, "gas_limit")
            if gas_limit is not None
            else self.estimate_gas_limit(
                input_symbol,
                output_symbol,
                include_approval=include_approval,
            )
        )
        gas_price = (
            self._coerce_positive_int(gas_price_wei, "gas_price_wei")
            if gas_price_wei is not None
            else self.get_gas_price_wei()
        )
        gas_cost_wei = limit * gas_price
        gas_cost_eth = gas_cost_wei / 1e18
        resolved_eth_price = self._resolve_eth_price(eth_price_usd)
        gas_cost_usd = (
            gas_cost_eth * resolved_eth_price if resolved_eth_price is not None else None
        )

        return GasEstimate(
            input_token=input_symbol,
            output_token=output_symbol,
            gas_limit=limit,
            gas_price_wei=gas_price,
            gas_cost_wei=gas_cost_wei,
            gas_cost_eth=gas_cost_eth,
            gas_cost_usd=gas_cost_usd,
            includes_approval=include_approval,
        )

    def evaluate_quote(
        self,
        quote: TradeQuote,
        *,
        input_price_usd: Optional[float] = None,
        output_price_usd: Optional[float] = None,
        gas_price_wei: Optional[int] = None,
        gas_limit: Optional[int] = None,
        eth_price_usd: Optional[float] = None,
        include_approval: bool = False,
        min_profit_usd: Optional[float] = None,
    ) -> TradeProfitability:
        """Determine whether a quoted trade remains profitable after L1 gas."""
        resolved_input_price = self._resolve_token_price(
            quote.input_token,
            explicit_price=input_price_usd,
            eth_price_usd=eth_price_usd,
        )
        resolved_output_price = self._resolve_token_price(
            quote.output_token,
            explicit_price=output_price_usd,
            eth_price_usd=eth_price_usd,
        )
        gas_estimate = self.estimate_gas_cost(
            quote.input_token,
            quote.output_token,
            gas_price_wei=gas_price_wei,
            gas_limit=gas_limit,
            eth_price_usd=eth_price_usd,
            include_approval=include_approval,
        )
        if gas_estimate.gas_cost_usd is None:
            raise RuntimeError("ETH/USD price is required to evaluate profitability")

        input_value_usd = quote.input_amount_units * resolved_input_price
        output_value_usd = quote.output_amount_units * resolved_output_price
        gross_profit_usd = output_value_usd - input_value_usd
        net_profit_usd = gross_profit_usd - gas_estimate.gas_cost_usd
        min_profit = (
            self._coerce_non_negative_float(min_profit_usd, "min_profit_usd")
            if min_profit_usd is not None
            else self.min_profit_usd
        )

        break_even_value_usd = input_value_usd + gas_estimate.gas_cost_usd + min_profit
        break_even_output_units = break_even_value_usd / resolved_output_price
        break_even_output_amount = self.to_raw(
            quote.output_token,
            break_even_output_units,
        )

        return TradeProfitability(
            quote=quote,
            gas_estimate=gas_estimate,
            input_value_usd=input_value_usd,
            output_value_usd=output_value_usd,
            gross_profit_usd=gross_profit_usd,
            net_profit_usd=net_profit_usd,
            min_profit_usd=min_profit,
            output_price_usd=resolved_output_price,
            break_even_output_amount=break_even_output_amount,
        )

    def evaluate_trade(
        self,
        input_token: str,
        output_token: str,
        amount_in: int | float,
        amount_out: int | float,
        *,
        amounts_are_raw: bool = True,
        route: str = "",
        price_impact_pct: float = 0.0,
        input_price_usd: Optional[float] = None,
        output_price_usd: Optional[float] = None,
        gas_price_wei: Optional[int] = None,
        gas_limit: Optional[int] = None,
        eth_price_usd: Optional[float] = None,
        include_approval: bool = False,
        min_profit_usd: Optional[float] = None,
    ) -> TradeProfitability:
        """Build a quote and evaluate its net profitability."""
        quote = self.build_quote(
            input_token,
            output_token,
            amount_in,
            amount_out,
            amounts_are_raw=amounts_are_raw,
            route=route,
            price_impact_pct=price_impact_pct,
        )
        return self.evaluate_quote(
            quote,
            input_price_usd=input_price_usd,
            output_price_usd=output_price_usd,
            gas_price_wei=gas_price_wei,
            gas_limit=gas_limit,
            eth_price_usd=eth_price_usd,
            include_approval=include_approval,
            min_profit_usd=min_profit_usd,
        )

    def is_trade_profitable(self, *args: Any, **kwargs: Any) -> bool:
        """Convenience wrapper that only returns the profitability decision."""
        return self.evaluate_trade(*args, **kwargs).profitable

    def _resolve_token_price(
        self,
        token: str,
        *,
        explicit_price: Optional[float],
        eth_price_usd: Optional[float],
    ) -> float:
        """Resolve a token USD price, defaulting stables to $1 and WETH to ETH."""
        symbol = self._normalize_token(token)
        if explicit_price is not None:
            return self._coerce_positive_float(explicit_price, f"{symbol} price")

        cached_price = self.token_prices_usd.get(symbol)
        if cached_price is not None:
            return cached_price

        if self.tokens[symbol].stable:
            return 1.0
        if symbol == "WETH":
            resolved = self._resolve_eth_price(eth_price_usd)
            if resolved is None:
                raise RuntimeError("ETH/USD price is required to price WETH")
            return resolved
        raise ValueError(f"Missing USD price for token {symbol}")

    def _resolve_eth_price(self, eth_price_usd: Optional[float]) -> Optional[float]:
        """Return an ETH/USD price from explicit, cached, or live sources."""
        if eth_price_usd is not None:
            return self._coerce_positive_float(eth_price_usd, "ETH/USD price")
        for key in ("WETH", "ETH"):
            if key in self.token_prices_usd:
                return self.token_prices_usd[key]
        if self.eth_usd_spot_url:
            return self.get_eth_price_usd()
        return None

    def _rpc(self, method: str, params: list[Any]) -> Any:
        """Execute a JSON-RPC request against the configured Ethereum endpoint."""
        if not self.rpc_url:
            raise RuntimeError(
                "ETHEREUM_RPC_URL is required for live Ethereum RPC requests"
            )

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }
        ).encode()
        req = urllib.request.Request(
            self.rpc_url,
            data=body,
            headers={
                "User-Agent": HEADERS.get("User-Agent", "JupiterSentinel/1.0"),
                "Content-Type": "application/json",
            },
        )
        payload = request_json(
            req,
            timeout=self.rpc_timeout,
            describe=f"Ethereum RPC {method}",
        )
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            if isinstance(error, dict):
                raise RuntimeError(str(error.get("message") or error))
            raise RuntimeError(str(error))
        if not isinstance(payload, dict) or "result" not in payload:
            raise RuntimeError(f"Unexpected Ethereum RPC payload for {method}")
        return payload["result"]

    def _normalize_token(self, token: str) -> str:
        """Normalize a token symbol or Ethereum address to a supported symbol."""
        text = str(token).strip()
        if not text:
            raise ValueError("token is required")

        upper = text.upper()
        if upper in self.tokens:
            return upper

        address = text.lower()
        symbol = self._address_index.get(address)
        if symbol is not None:
            return symbol

        raise ValueError(f"Unsupported Ethereum token: {token}")

    def _normalize_price_key(self, token: str) -> str:
        """Accept ETH as an alias when seeding local price hints."""
        text = str(token).strip()
        if not text:
            raise ValueError("token price key is required")
        if text.upper() == "ETH":
            return "WETH"
        return self._normalize_token(text)

    def _coerce_positive_int(self, value: object, field: str) -> int:
        """Validate positive integer inputs."""
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a positive integer") from exc
        if normalized <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return normalized

    def _coerce_positive_float(self, value: object, field: str) -> float:
        """Validate positive float inputs."""
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be finite and > 0") from exc
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError(f"{field} must be finite and > 0")
        return normalized

    def _coerce_non_negative_float(self, value: object, field: str) -> float:
        """Validate non-negative float inputs."""
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be finite and >= 0") from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{field} must be finite and >= 0")
        return normalized


EthereumMainnet = EthereumChain


def estimate_gas_cost(
    input_token: str,
    output_token: str,
    *,
    gas_price_wei: Optional[int] = None,
    gas_limit: Optional[int] = None,
    eth_price_usd: Optional[float] = None,
    include_approval: bool = False,
    rpc_url: Optional[str] = None,
    rpc_timeout: int = DEFAULT_RPC_TIMEOUT,
    token_prices_usd: Optional[Mapping[str, float]] = None,
) -> GasEstimate:
    """Module-level gas estimate helper."""
    chain = EthereumChain(
        rpc_url=rpc_url,
        rpc_timeout=rpc_timeout,
        token_prices_usd=token_prices_usd,
    )
    return chain.estimate_gas_cost(
        input_token,
        output_token,
        gas_price_wei=gas_price_wei,
        gas_limit=gas_limit,
        eth_price_usd=eth_price_usd,
        include_approval=include_approval,
    )


def evaluate_trade_profitability(
    input_token: str,
    output_token: str,
    amount_in: int | float,
    amount_out: int | float,
    *,
    amounts_are_raw: bool = True,
    route: str = "",
    price_impact_pct: float = 0.0,
    input_price_usd: Optional[float] = None,
    output_price_usd: Optional[float] = None,
    gas_price_wei: Optional[int] = None,
    gas_limit: Optional[int] = None,
    eth_price_usd: Optional[float] = None,
    include_approval: bool = False,
    min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
    rpc_url: Optional[str] = None,
    rpc_timeout: int = DEFAULT_RPC_TIMEOUT,
    token_prices_usd: Optional[Mapping[str, float]] = None,
) -> TradeProfitability:
    """Module-level trade-profitability helper."""
    chain = EthereumChain(
        rpc_url=rpc_url,
        rpc_timeout=rpc_timeout,
        min_profit_usd=min_profit_usd,
        token_prices_usd=token_prices_usd,
    )
    return chain.evaluate_trade(
        input_token,
        output_token,
        amount_in,
        amount_out,
        amounts_are_raw=amounts_are_raw,
        route=route,
        price_impact_pct=price_impact_pct,
        input_price_usd=input_price_usd,
        output_price_usd=output_price_usd,
        gas_price_wei=gas_price_wei,
        gas_limit=gas_limit,
        eth_price_usd=eth_price_usd,
        include_approval=include_approval,
    )


def is_trade_profitable(*args: Any, **kwargs: Any) -> bool:
    """Return only the boolean trade decision."""
    return evaluate_trade_profitability(*args, **kwargs).profitable


__all__ = [
    "CHAIN_ID",
    "CHAIN_NAME",
    "DEFAULT_APPROVAL_GAS_LIMIT",
    "DEFAULT_GAS_LIMITS",
    "ETH_SYMBOL",
    "ETH_USD_SPOT_URL",
    "EthereumChain",
    "EthereumMainnet",
    "GasEstimate",
    "SUPPORTED_PAIRS",
    "SUPPORTED_TOKENS",
    "Token",
    "TradeProfitability",
    "TradeQuote",
    "USDC",
    "USDT",
    "WETH",
    "estimate_gas_cost",
    "evaluate_trade_profitability",
    "is_trade_profitable",
]
