"""
Jupiter free-tier limits and bot runtime policy.

Verified against official Jupiter documentation on 2026-04-13.

Primary sources:
- Developer Platform pricing: https://developers.jup.ag/pricing
- Developer docs overview: https://developers.jup.ag/docs/get-started
- Swap routing: https://developers.jup.ag/docs/swap/routing
- Tokens guide: https://developers.jup.ag/docs/guides/how-to-get-token-information
- Trigger guide: https://developers.jup.ag/docs/trigger
- Legacy Metis quote reference: https://dev.jup.ag/docs/swap/v1/get-quote
- Legacy portal rate-limit reference: https://dev.jup.ag/portal/rate-limit

Why this module exists:
- Jupiter's public docs are in the middle of a platform migration.
- The current pricing page publishes Free = 1 request/second.
- The still-linked legacy portal docs publish Free = 60 requests / 60 seconds.

Those two statements are equivalent throughput. This module normalizes the free
plan to a conservative shared 60 requests / 60 second budget so the bot stays
under both representations of the published free tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, floor

JUPITER_LIMITS_LAST_VERIFIED = "2026-04-13"

DEVELOPER_PLATFORM_PRICING_URL = "https://developers.jup.ag/pricing"
DEVELOPER_DOCS_URL = "https://developers.jup.ag/docs/get-started"
SWAP_ROUTING_DOCS_URL = "https://developers.jup.ag/docs/swap/routing"
TOKENS_GUIDE_URL = "https://developers.jup.ag/docs/guides/how-to-get-token-information"
TRIGGER_DOCS_URL = "https://developers.jup.ag/docs/trigger"
LEGACY_QUOTE_DOCS_URL = "https://dev.jup.ag/docs/swap/v1/get-quote"
LEGACY_RATE_LIMIT_DOCS_URL = "https://dev.jup.ag/portal/rate-limit"

# Current free-tier publication on the Developer Platform pricing page.
FREE_PLAN_REQUESTS_PER_SECOND = 1

# Legacy portal docs still linked from the docs during migration.
FREE_PLAN_REQUESTS_PER_MINUTE = 60
FREE_PLAN_WINDOW_SECONDS = 60

# Operate below the published ceiling so execution retries and wallet actions
# still have headroom when the scanner is busy.
FREE_PLAN_SAFE_UTILIZATION = 0.85
FREE_PLAN_SAFE_REQUESTS_PER_MINUTE = floor(
    FREE_PLAN_REQUESTS_PER_MINUTE * FREE_PLAN_SAFE_UTILIZATION
)

PRICE_API_MAX_IDS_PER_REQUEST = 50
TOKENS_SEARCH_MAX_QUERIES_PER_REQUEST = 100
TOKENS_INDEXED_COUNT_FLOOR = 580_000
TRIGGER_V2_MIN_ORDER_USD = 10.0
JUPITER_MAX_SWAP_AMOUNT_U64 = (1 << 64) - 1
LEGACY_RECOMMENDED_MAX_ACCOUNTS = 64


@dataclass(frozen=True, slots=True)
class SlidingWindowLimit:
    """Published request limit for a Jupiter bucket."""

    requests: int
    window_seconds: int
    scope: str
    notes: tuple[str, ...] = ()

    @property
    def requests_per_second(self) -> float:
        """Equivalent steady-state request rate."""
        return self.requests / self.window_seconds


@dataclass(frozen=True, slots=True)
class JupiterEndpointCapability:
    """What one free-tier endpoint can do and how the bot should treat it."""

    endpoint: str
    rate_limit: SlidingWindowLimit
    route_types: tuple[str, ...] = ()
    max_amount_raw: int | None = None
    max_items_per_request: int | None = None
    can_do: tuple[str, ...] = ()
    cannot_do: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JupiterFreeTierConfig:
    """Derived workload plan for a bot running on the free plan."""

    shared_rate_limit: SlidingWindowLimit
    safe_requests_per_minute: int
    requested_scan_pairs: int
    requested_scan_interval_seconds: int
    quote_requests_per_pair: int
    reserve_execution_requests_per_minute: int
    reserve_monitoring_requests_per_minute: int
    reserve_metadata_requests_per_minute: int
    scan_budget_requests_per_minute: int
    effective_scan_interval_seconds: int
    max_pairs_per_scan: int
    quote_batch_window_seconds: float
    price_ids_per_request: int
    token_queries_per_request: int
    prefer_swap_endpoint: str
    prefer_order_mode: str
    prefer_restrict_intermediate_tokens: bool
    allow_exact_out: bool
    allow_only_direct_routes: bool
    recommended_max_accounts: int
    trigger_min_order_usd: float
    max_swap_amount_raw: int
    notes: tuple[str, ...] = field(default_factory=tuple)


FREE_PLAN_SHARED_LIMIT = SlidingWindowLimit(
    requests=FREE_PLAN_REQUESTS_PER_MINUTE,
    window_seconds=FREE_PLAN_WINDOW_SECONDS,
    scope="account-wide shared fixed bucket",
    notes=(
        "Developer Platform pricing publishes Free = 1 request/second.",
        "Legacy portal docs publish Free = 60 requests per 60 seconds.",
        "The bot uses the stricter shared 60 RPM interpretation for safety.",
    ),
)

JUPITER_FREE_ENDPOINT_CAPABILITIES: dict[str, JupiterEndpointCapability] = {
    "swap_v2_order": JupiterEndpointCapability(
        endpoint="/swap/v2/order",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        route_types=("ultra", "manual"),
        max_amount_raw=JUPITER_MAX_SWAP_AMOUNT_U64,
        can_do=(
            "Quote and assemble a transaction in one call.",
            "Use all routers when no optional params are passed.",
            "Return quote-only responses when taker is omitted.",
        ),
        cannot_do=(
            "Guarantee full router access if optional params force manual mode.",
        ),
        notes=(
            "Preferred free-tier execution endpoint.",
            "Ultra mode = Metis + JupiterZ + Dflow + OKX.",
            "Manual mode = routing may be restricted by optional params.",
        ),
    ),
    "swap_v2_execute": JupiterEndpointCapability(
        endpoint="/swap/v2/execute",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        can_do=(
            "Submit a signed order transaction through Jupiter-managed landing.",
        ),
        notes=(
            "Pairs with /swap/v2/order.",
            "Uses the same free shared request budget.",
        ),
    ),
    "swap_v2_build": JupiterEndpointCapability(
        endpoint="/swap/v2/build",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        route_types=("manual-metis-only",),
        max_amount_raw=JUPITER_MAX_SWAP_AMOUNT_U64,
        can_do=(
            "Build raw swap instructions for custom transactions.",
        ),
        cannot_do=(
            "Access JupiterZ RFQ or third-party routers.",
        ),
        notes=(
            "Metis-only route construction.",
            "Use when you need custom transaction composition.",
        ),
    ),
    "swap_v1_quote": JupiterEndpointCapability(
        endpoint="/swap/v1/quote",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        route_types=(
            "ExactIn",
            "ExactOut",
            "multi-hop",
            "multi-split",
            "onlyDirectRoutes",
        ),
        max_amount_raw=JUPITER_MAX_SWAP_AMOUNT_U64,
        can_do=(
            "Return legacy Metis quotes.",
            "Support ExactIn and ExactOut swap modes.",
            "Restrict routes to direct-only when requested.",
        ),
        cannot_do=(
            "Access JupiterZ RFQ or third-party routers.",
            "Guarantee ExactOut on every AMM.",
        ),
        notes=(
            "Legacy endpoint; Jupiter says Swap V2 supersedes it.",
            "ExactOut is only supported on some AMMs and is not recommended for most use cases.",
            "restrictIntermediateTokens improves route stability.",
            "Keep maxAccounts as high as possible; Jupiter recommends 64.",
        ),
    ),
    "swap_v1_swap": JupiterEndpointCapability(
        endpoint="/swap/v1/swap",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        max_amount_raw=JUPITER_MAX_SWAP_AMOUNT_U64,
        can_do=("Build a legacy swap transaction from a quote response.",),
        cannot_do=("Access non-Metis routers.",),
        notes=("Legacy execution path only.",),
    ),
    "program_id_to_label": JupiterEndpointCapability(
        endpoint="/swap/v1/program-id-to-label",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        can_do=("Map AMM program IDs to DEX labels.",),
        notes=("Useful for route intelligence and quote debugging.",),
    ),
    "tokens_v2_search": JupiterEndpointCapability(
        endpoint="/tokens/v2/search",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        max_items_per_request=TOKENS_SEARCH_MAX_QUERIES_PER_REQUEST,
        can_do=(
            "Search by mint, symbol, or name.",
            "Resolve up to 100 comma-separated queries per request.",
            "Query a token index covering 580k+ tokens.",
        ),
        cannot_do=(
            "Guarantee every indexed token is tradeable via swap routes.",
        ),
        notes=(
            "580k+ indexed token count comes from Jupiter's official token guide.",
        ),
    ),
    "tokens_v2_tag": JupiterEndpointCapability(
        endpoint="/tokens/v2/tag",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        can_do=("Filter tokens by tags such as verified or lst.",),
    ),
    "tokens_v2_top": JupiterEndpointCapability(
        endpoint="/tokens/v2/{category}/{interval}",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        can_do=(
            "Fetch top trending, top traded, and top organic-score token lists.",
        ),
        notes=("Intervals: 5m, 1h, 6h, 24h.",),
    ),
    "tokens_v2_recent": JupiterEndpointCapability(
        endpoint="/tokens/v2/recent",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        can_do=("Fetch recently listed tokens.",),
    ),
    "price_v3": JupiterEndpointCapability(
        endpoint="/price/v3",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        max_items_per_request=PRICE_API_MAX_IDS_PER_REQUEST,
        can_do=("Price up to 50 tokens per request.",),
        notes=(
            "Legacy portal docs say free users do not receive a separate Price bucket.",
        ),
    ),
    "trigger_v2": JupiterEndpointCapability(
        endpoint="/trigger/v2",
        rate_limit=FREE_PLAN_SHARED_LIMIT,
        route_types=("single", "OCO", "OTOCO"),
        can_do=(
            "Create advanced trigger orders using USD price triggers.",
            "Support single, OCO, and OTOCO orders.",
            "Edit trigger price and slippage in place.",
            "Support partial fills.",
        ),
        cannot_do=(
            "Create orders smaller than 10 USD equivalent.",
        ),
        notes=(
            "Authenticated flows require both API key and JWT.",
            "Trigger orders execute against current Jupiter routing liquidity, so output is not guaranteed.",
        ),
    ),
}


def build_free_tier_bot_config(
    *,
    requested_scan_pairs: int,
    requested_scan_interval_seconds: int,
    quote_requests_per_pair: int = 1,
    reserve_execution_requests_per_minute: int = 6,
    reserve_monitoring_requests_per_minute: int = 12,
    reserve_metadata_requests_per_minute: int = 4,
    safety_utilization: float = FREE_PLAN_SAFE_UTILIZATION,
    quote_batch_window_seconds: float = 0.05,
) -> JupiterFreeTierConfig:
    """
    Build a conservative runtime policy for the current free plan.

    The plan deliberately leaves room for:
    - live execution calls
    - quote retries
    - background wallet / metadata work
    """
    if requested_scan_pairs < 0:
        raise ValueError("requested_scan_pairs cannot be negative")
    if requested_scan_interval_seconds <= 0:
        raise ValueError("requested_scan_interval_seconds must be positive")
    if quote_requests_per_pair <= 0:
        raise ValueError("quote_requests_per_pair must be positive")
    if not 0 < safety_utilization <= 1:
        raise ValueError("safety_utilization must be within (0, 1]")

    safe_requests_per_minute = max(
        1, floor(FREE_PLAN_REQUESTS_PER_MINUTE * safety_utilization)
    )
    reserved_requests = (
        reserve_execution_requests_per_minute
        + reserve_monitoring_requests_per_minute
        + reserve_metadata_requests_per_minute
    )
    scan_budget_requests_per_minute = max(1, safe_requests_per_minute - reserved_requests)

    if requested_scan_pairs == 0:
        return JupiterFreeTierConfig(
            shared_rate_limit=FREE_PLAN_SHARED_LIMIT,
            safe_requests_per_minute=safe_requests_per_minute,
            requested_scan_pairs=0,
            requested_scan_interval_seconds=requested_scan_interval_seconds,
            quote_requests_per_pair=quote_requests_per_pair,
            reserve_execution_requests_per_minute=reserve_execution_requests_per_minute,
            reserve_monitoring_requests_per_minute=reserve_monitoring_requests_per_minute,
            reserve_metadata_requests_per_minute=reserve_metadata_requests_per_minute,
            scan_budget_requests_per_minute=scan_budget_requests_per_minute,
            effective_scan_interval_seconds=requested_scan_interval_seconds,
            max_pairs_per_scan=0,
            quote_batch_window_seconds=quote_batch_window_seconds,
            price_ids_per_request=PRICE_API_MAX_IDS_PER_REQUEST,
            token_queries_per_request=TOKENS_SEARCH_MAX_QUERIES_PER_REQUEST,
            prefer_swap_endpoint="/swap/v2/order",
            prefer_order_mode="ultra",
            prefer_restrict_intermediate_tokens=True,
            allow_exact_out=False,
            allow_only_direct_routes=False,
            recommended_max_accounts=LEGACY_RECOMMENDED_MAX_ACCOUNTS,
            trigger_min_order_usd=TRIGGER_V2_MIN_ORDER_USD,
            max_swap_amount_raw=JUPITER_MAX_SWAP_AMOUNT_U64,
            notes=("No scan pairs requested.",),
        )

    requested_quotes_per_cycle = requested_scan_pairs * quote_requests_per_pair
    minimum_interval_seconds = ceil(
        (requested_quotes_per_cycle * 60) / scan_budget_requests_per_minute
    )
    effective_scan_interval_seconds = max(
        requested_scan_interval_seconds,
        minimum_interval_seconds,
    )
    max_pairs_per_scan = max(
        1,
        floor(
            (scan_budget_requests_per_minute * effective_scan_interval_seconds)
            / (60 * quote_requests_per_pair)
        ),
    )
    max_pairs_per_scan = min(max_pairs_per_scan, requested_scan_pairs)

    notes: list[str] = [
        "Prefer /swap/v2/order + /swap/v2/execute for live execution because it preserves full router competition on the happy path.",
        "Keep quote traffic on ExactIn unless you have a strict ExactOut payment use case.",
        "Avoid onlyDirectRoutes for production trading; it can produce no quote or worse prices.",
        "Set restrictIntermediateTokens=true for bot quotes that prioritize route stability over route exploration.",
    ]
    if effective_scan_interval_seconds > requested_scan_interval_seconds:
        notes.append(
            "Raised scan interval to stay under the free shared request budget."
        )
    if max_pairs_per_scan < requested_scan_pairs:
        notes.append(
            "Not all tracked pairs can be quoted every cycle at the requested interval; rotate or trim scan pairs."
        )

    return JupiterFreeTierConfig(
        shared_rate_limit=FREE_PLAN_SHARED_LIMIT,
        safe_requests_per_minute=safe_requests_per_minute,
        requested_scan_pairs=requested_scan_pairs,
        requested_scan_interval_seconds=requested_scan_interval_seconds,
        quote_requests_per_pair=quote_requests_per_pair,
        reserve_execution_requests_per_minute=reserve_execution_requests_per_minute,
        reserve_monitoring_requests_per_minute=reserve_monitoring_requests_per_minute,
        reserve_metadata_requests_per_minute=reserve_metadata_requests_per_minute,
        scan_budget_requests_per_minute=scan_budget_requests_per_minute,
        effective_scan_interval_seconds=effective_scan_interval_seconds,
        max_pairs_per_scan=max_pairs_per_scan,
        quote_batch_window_seconds=quote_batch_window_seconds,
        price_ids_per_request=PRICE_API_MAX_IDS_PER_REQUEST,
        token_queries_per_request=TOKENS_SEARCH_MAX_QUERIES_PER_REQUEST,
        prefer_swap_endpoint="/swap/v2/order",
        prefer_order_mode="ultra",
        prefer_restrict_intermediate_tokens=True,
        allow_exact_out=False,
        allow_only_direct_routes=False,
        recommended_max_accounts=LEGACY_RECOMMENDED_MAX_ACCOUNTS,
        trigger_min_order_usd=TRIGGER_V2_MIN_ORDER_USD,
        max_swap_amount_raw=JUPITER_MAX_SWAP_AMOUNT_U64,
        notes=tuple(notes),
    )
