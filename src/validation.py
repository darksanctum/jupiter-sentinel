"""
Validation helpers for untrusted configuration and request parameters.
"""

from __future__ import annotations
import logging

import ipaddress
import math
import re
from numbers import Real
from typing import Any
from urllib.parse import urlencode


_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def validate_solana_address(value: Any, field_name: str = "address") -> str:
    """Validate a base58-encoded Solana address-like value."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    if not _SOLANA_ADDRESS_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a valid base58 Solana address")
    return normalized


def validate_int(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Validate integer request parameters such as lamports and basis points."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field_name} must be an integer")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")

    normalized = int(value)
    if normalized != value:
        raise ValueError(f"{field_name} must be an integer")
    if minimum is not None and normalized < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}")
    return normalized


def validate_port(value: Any) -> int:
    """Validate a TCP port for local dashboard binding."""
    return validate_int(value, "port", minimum=1, maximum=65535)


def validate_host(value: Any) -> str:
    """Validate a hostname/IP string used for local server binding."""
    if not isinstance(value, str):
        raise ValueError("host must be a string")

    normalized = value.strip()
    if not normalized:
        raise ValueError("host cannot be empty")

    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass

    if normalized == "localhost" or _HOSTNAME_RE.fullmatch(normalized):
        return normalized
    raise ValueError("host must be localhost, a valid hostname, or an IP address")


def build_jupiter_quote_url(
    base_url: str,
    input_mint: Any,
    output_mint: Any,
    amount: Any,
    slippage_bps: Any = 50,
    *,
    only_direct_routes: bool | None = None,
    as_legacy_transaction: bool | None = None,
) -> str:
    """Build a Jupiter quote URL from validated, encoded parameters."""
    params: list[tuple[str, str]] = [
        ("inputMint", validate_solana_address(input_mint, "input_mint")),
        ("outputMint", validate_solana_address(output_mint, "output_mint")),
        ("amount", str(validate_int(amount, "amount", minimum=1))),
        (
            "slippageBps",
            str(validate_int(slippage_bps, "slippage_bps", minimum=0, maximum=10_000)),
        ),
    ]

    if only_direct_routes is not None:
        params.append(("onlyDirectRoutes", "true" if only_direct_routes else "false"))
    if as_legacy_transaction is not None:
        params.append(
            ("asLegacyTransaction", "true" if as_legacy_transaction else "false")
        )

    return f"{base_url}/quote?{urlencode(params)}"
