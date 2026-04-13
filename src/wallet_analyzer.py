"""
Wallet trade history analyzer backed by Solana RPC.

The analyzer fetches a wallet's full transaction history via
`getSignaturesForAddress` + `getTransaction`, infers swap executions from net
wallet balance deltas, and builds a FIFO lot ledger for realized trade
analytics.

Important accounting note:
- Realized P&L is only computed when the entry and exit quote assets are
  directly comparable from RPC data alone.
- Stablecoin exits are normalized into `USD`.
- Native SOL exits are normalized into `SOL`.
- When a token is bought in one quote asset and sold in another, the analyzer
  records the closure but marks P&L as unresolved instead of inventing a
  conversion rate.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .config import (
    BONK_MINT,
    JUP_MINT,
    PROJECT_DIR,
    RPC_URL,
    SOL_MINT,
    USDC_MINT,
    WIF_MINT,
    get_pubkey,
)
from .resilience import atomic_write_text, rpc_request
from .validation import validate_solana_address


LOGGER = logging.getLogger(__name__)

WSOL_DECIMALS = 9
MAX_SIGNATURES_PER_PAGE = 1_000
MIN_ASSET_DELTA = 1e-12
SECONDARY_DELTA_RATIO = 0.05

USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

DEFAULT_STABLE_MINTS = frozenset({USDC_MINT, USDT_MINT})
DEFAULT_SYMBOLS = {
    SOL_MINT: "SOL",
    USDC_MINT: "USDC",
    USDT_MINT: "USDT",
    JUP_MINT: "JUP",
    BONK_MINT: "BONK",
    WIF_MINT: "WIF",
}


@dataclass(frozen=True)
class AssetDelta:
    """Net wallet delta for one asset in a single transaction."""

    mint: str
    symbol: str
    amount: float


@dataclass(frozen=True)
class SwapRecord:
    """A wallet-relative swap inferred from net balance changes."""

    signature: str
    timestamp: datetime
    sold: AssetDelta
    bought: AssetDelta
    fee_sol: float
    slot: Optional[int] = None
    raw_deltas: dict[str, float] = field(default_factory=dict)
    ignored_deltas: dict[str, float] = field(default_factory=dict)


@dataclass
class InventoryLot:
    """Open inventory lot for FIFO matching."""

    quantity: float
    acquired_at: datetime
    cost_amount: float
    cost_mint: str
    cost_currency: str
    entry_signature: str


@dataclass(frozen=True)
class ClosedTrade:
    """A FIFO-matched token closure."""

    token_mint: str
    token_symbol: str
    quantity: float
    opened_at: datetime
    closed_at: datetime
    cost_amount: float
    cost_currency: str
    proceeds_amount: float
    proceeds_currency: str
    realized_pnl: Optional[float]
    return_pct: Optional[float]
    entry_signature: str
    exit_signature: str
    status: str = "matched"

    @property
    def hold_seconds(self) -> float:
        """Return the holding period in seconds."""
        return max((self.closed_at - self.opened_at).total_seconds(), 0.0)

    @property
    def comparable_currency(self) -> Optional[str]:
        """Return the comparable quote currency for matched trades."""
        if self.realized_pnl is None:
            return None
        return self.proceeds_currency


@dataclass
class TokenTradeSummary:
    """Per-token trade summary derived from matched closures."""

    mint: str
    symbol: str
    closed_trades: int = 0
    comparable_trades: int = 0
    unresolved_trades: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    realized_pnl_by_currency: dict[str, float] = field(default_factory=dict)
    hold_seconds_total: float = 0.0
    hold_samples: int = 0
    return_pct_total: float = 0.0
    return_pct_samples: int = 0
    open_quantity: float = 0.0

    @property
    def average_hold_seconds(self) -> Optional[float]:
        """Return mean hold time across closures with known entry lots."""
        if self.hold_samples == 0:
            return None
        return self.hold_seconds_total / self.hold_samples

    @property
    def average_return_pct(self) -> Optional[float]:
        """Return mean realized return across comparable closures."""
        if self.return_pct_samples == 0:
            return None
        return self.return_pct_total / self.return_pct_samples


@dataclass(frozen=True)
class WalletAnalysis:
    """Top-level result for a wallet history analysis pass."""

    wallet_address: str
    fetched_signatures: int
    analyzed_transactions: int
    swap_count: int
    total_fee_sol: float
    swaps: list[SwapRecord]
    closed_trades: list[ClosedTrade]
    token_summaries: list[TokenTradeSummary]

    @property
    def comparable_closed_trades(self) -> list[ClosedTrade]:
        """Return closures with directly comparable realized P&L."""
        return [trade for trade in self.closed_trades if trade.realized_pnl is not None]

    @property
    def unresolved_closed_trades(self) -> list[ClosedTrade]:
        """Return closures that could not be normalized safely."""
        return [trade for trade in self.closed_trades if trade.realized_pnl is None]

    @property
    def best_trade(self) -> Optional[ClosedTrade]:
        """Return the highest-return comparable closure."""
        candidates = [trade for trade in self.closed_trades if trade.return_pct is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda trade: float(trade.return_pct or 0.0))

    @property
    def worst_trade(self) -> Optional[ClosedTrade]:
        """Return the lowest-return comparable closure."""
        candidates = [trade for trade in self.closed_trades if trade.return_pct is not None]
        if not candidates:
            return None
        return min(candidates, key=lambda trade: float(trade.return_pct or 0.0))


def _amount_from_ui_token(balance: Mapping[str, Any]) -> tuple[int, int]:
    """Extract raw integer amount and decimals from a token balance payload."""
    ui_amount = balance.get("uiTokenAmount") or {}
    raw_amount = int(str(ui_amount.get("amount", "0") or "0"))
    decimals = int(ui_amount.get("decimals", 0) or 0)
    return raw_amount, decimals


def _account_key_to_str(value: Any) -> str:
    """Normalize parsed and unparsed account key entries to strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("pubkey", "") or "")
    return str(value or "")


def _symbol_for_mint(mint: str, symbol_overrides: Mapping[str, str]) -> str:
    """Return a readable symbol fallback for a mint."""
    if mint in symbol_overrides:
        return symbol_overrides[mint]
    if mint == SOL_MINT:
        return "SOL"
    return mint[:4] + "..." + mint[-4:]


def _format_timedelta(seconds: Optional[float]) -> str:
    """Format a duration in a compact, report-friendly way."""
    if seconds is None:
        return "n/a"

    value = max(float(seconds), 0.0)
    if value < 60:
        return f"{value:.0f}s"

    minutes = value / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"

    hours = value / 3600.0
    if hours < 48:
        return f"{hours:.1f}h"

    days = value / 86400.0
    return f"{days:.1f}d"


def _format_quote_amount(value: float, currency: str) -> str:
    """Render an amount for markdown output."""
    if currency == "USD":
        return f"${value:.2f}"
    if currency == "SOL":
        return f"{value:.6f} SOL"
    return f"{value:.6f} {currency}"


def _format_pnl_map(pnl_by_currency: Mapping[str, float]) -> str:
    """Format a mixed-currency realized P&L bucket."""
    if not pnl_by_currency:
        return "n/a"
    ordered = []
    for currency in sorted(pnl_by_currency):
        ordered.append(_format_quote_amount(pnl_by_currency[currency], currency))
    return "; ".join(ordered)


def _redact_address(address: str) -> str:
    """Return a shortened wallet label fit for reports."""
    if len(address) <= 10:
        return address
    return f"{address[:4]}...{address[-4:]}"


class WalletAnalyzer:
    """Fetch wallet history from Solana RPC and compute trade analytics."""

    def __init__(
        self,
        wallet_address: str,
        *,
        rpc_url: str = RPC_URL,
        stable_mints: Iterable[str] = DEFAULT_STABLE_MINTS,
        symbol_overrides: Optional[Mapping[str, str]] = None,
        timeout: float = 20.0,
    ) -> None:
        self.wallet_address = validate_solana_address(wallet_address, "wallet_address")
        self.rpc_url = rpc_url
        self.stable_mints = frozenset(stable_mints)
        self.symbol_overrides = dict(DEFAULT_SYMBOLS)
        self.symbol_overrides.update(symbol_overrides or {})
        self.timeout = float(timeout)

    def _quote_currency(self, mint: str) -> str:
        """Normalize quote assets into report currencies."""
        if mint in self.stable_mints:
            return "USD"
        if mint == SOL_MINT:
            return "SOL"
        return _symbol_for_mint(mint, self.symbol_overrides)

    def _is_quote_asset(self, mint: str) -> bool:
        """Return whether the mint is treated as a funding/quote asset."""
        return mint == SOL_MINT or mint in self.stable_mints

    def fetch_signatures(
        self, *, max_signatures: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """Fetch finalized wallet signatures via paginated Solana RPC calls."""
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        before: Optional[str] = None

        while max_signatures is None or len(collected) < max_signatures:
            limit = MAX_SIGNATURES_PER_PAGE
            if max_signatures is not None:
                limit = min(limit, max_signatures - len(collected))
            if limit <= 0:
                break

            options: dict[str, Any] = {"limit": limit, "commitment": "finalized"}
            if before:
                options["before"] = before

            batch = rpc_request(
                "getSignaturesForAddress",
                [self.wallet_address, options],
                rpc_url=self.rpc_url,
                timeout=self.timeout,
                describe="Solana getSignaturesForAddress",
            )
            if not isinstance(batch, list) or not batch:
                break

            for item in batch:
                if not isinstance(item, dict):
                    continue
                if item.get("err") not in (None, "", {}):
                    continue
                signature = str(item.get("signature", "") or "").strip()
                if not signature or signature in seen:
                    continue
                seen.add(signature)
                collected.append(item)
                if max_signatures is not None and len(collected) >= max_signatures:
                    break

            before = str((batch[-1] or {}).get("signature", "") or "")
            if len(batch) < limit or not before:
                break

        return collected

    def fetch_transaction(self, signature: str) -> Optional[dict[str, Any]]:
        """Fetch one transaction payload with parsed account metadata."""
        result = rpc_request(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
            rpc_url=self.rpc_url,
            timeout=self.timeout,
            describe="Solana getTransaction",
        )
        return result if isinstance(result, dict) else None

    def fetch_transactions(
        self, signature_infos: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Fetch parsed transaction payloads for the provided signatures."""
        transactions: list[dict[str, Any]] = []
        for info in signature_infos:
            signature = str(info.get("signature", "") or "").strip()
            if not signature:
                continue
            payload = self.fetch_transaction(signature)
            if payload is None:
                LOGGER.debug("Skipping transaction %s because RPC returned null", signature)
                continue
            transactions.append(payload)
        return transactions

    def _extract_wallet_deltas(self, transaction: Mapping[str, Any]) -> tuple[dict[str, float], float]:
        """Compute net wallet asset deltas for one parsed transaction."""
        meta = transaction.get("meta") or {}
        message = ((transaction.get("transaction") or {}).get("message") or {})
        account_keys = message.get("accountKeys") or []

        wallet_index = None
        for index, account_key in enumerate(account_keys):
            if _account_key_to_str(account_key) == self.wallet_address:
                wallet_index = index
                break

        deltas: dict[str, float] = defaultdict(float)
        fee_lamports = int(meta.get("fee", 0) or 0)

        if wallet_index is not None:
            pre_balances = meta.get("preBalances") or []
            post_balances = meta.get("postBalances") or []
            if wallet_index < len(pre_balances) and wallet_index < len(post_balances):
                native_delta_lamports = (
                    int(post_balances[wallet_index] or 0)
                    - int(pre_balances[wallet_index] or 0)
                    + fee_lamports
                )
                native_delta = native_delta_lamports / float(10**WSOL_DECIMALS)
                if abs(native_delta) > MIN_ASSET_DELTA:
                    deltas[SOL_MINT] += native_delta

        token_totals: dict[str, list[int]] = {}
        for phase_index, balances in enumerate(
            (meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or [])
        ):
            for balance in balances:
                if not isinstance(balance, Mapping):
                    continue
                owner = str(balance.get("owner", "") or "")
                if owner and owner != self.wallet_address:
                    continue

                mint = str(balance.get("mint", "") or "").strip()
                if not mint:
                    continue

                raw_amount, decimals = _amount_from_ui_token(balance)
                bucket = token_totals.setdefault(mint, [0, 0, decimals])
                bucket[phase_index] += raw_amount
                bucket[2] = decimals

        for mint, (pre_raw, post_raw, decimals) in token_totals.items():
            delta = (post_raw - pre_raw) / float(10**decimals if decimals >= 0 else 1)
            if abs(delta) > MIN_ASSET_DELTA:
                deltas[mint] += delta

        return dict(deltas), fee_lamports / float(10**WSOL_DECIMALS)

    def _looks_like_swap(
        self,
        positives: Sequence[AssetDelta],
        negatives: Sequence[AssetDelta],
        ignored_total: float,
        transaction: Mapping[str, Any],
    ) -> bool:
        """Apply a conservative heuristic before classifying a tx as a swap."""
        if not positives or not negatives:
            return False
        if len(positives) == 1 and len(negatives) == 1:
            return True

        primary = max(abs(positives[0].amount), abs(negatives[0].amount))
        if primary <= 0:
            return False
        if ignored_total <= primary * SECONDARY_DELTA_RATIO:
            return True

        logs = ((transaction.get("meta") or {}).get("logMessages") or [])
        for line in logs:
            if "swap" in str(line).lower():
                return True
        return False

    def parse_swap(self, transaction: Mapping[str, Any]) -> Optional[SwapRecord]:
        """Infer one wallet-relative swap from a parsed transaction payload."""
        meta = transaction.get("meta") or {}
        if meta.get("err") not in (None, "", {}):
            return None

        raw_deltas, fee_sol = self._extract_wallet_deltas(transaction)
        if not raw_deltas:
            return None

        normalized: list[AssetDelta] = []
        for mint, amount in raw_deltas.items():
            if abs(amount) <= MIN_ASSET_DELTA:
                continue
            normalized.append(
                AssetDelta(
                    mint=mint,
                    symbol=_symbol_for_mint(mint, self.symbol_overrides),
                    amount=amount,
                )
            )

        positives = sorted(
            [delta for delta in normalized if delta.amount > 0],
            key=lambda delta: abs(delta.amount),
            reverse=True,
        )
        negatives = sorted(
            [delta for delta in normalized if delta.amount < 0],
            key=lambda delta: abs(delta.amount),
            reverse=True,
        )
        if not positives or not negatives:
            return None

        primary_bought = positives[0]
        primary_sold = negatives[0]

        ignored: dict[str, float] = {}
        ignored_total = 0.0
        for delta in positives[1:] + negatives[1:]:
            ignored[delta.symbol] = delta.amount
            ignored_total += abs(delta.amount)

        if not self._looks_like_swap(positives, negatives, ignored_total, transaction):
            return None

        block_time = transaction.get("blockTime")
        timestamp = (
            datetime.utcfromtimestamp(int(block_time))
            if block_time not in (None, "")
            else datetime.utcfromtimestamp(0)
        )
        signature = ""
        signatures = ((transaction.get("transaction") or {}).get("signatures") or [])
        if signatures:
            signature = str(signatures[0] or "")

        return SwapRecord(
            signature=signature,
            timestamp=timestamp,
            sold=AssetDelta(
                mint=primary_sold.mint,
                symbol=primary_sold.symbol,
                amount=abs(primary_sold.amount),
            ),
            bought=primary_bought,
            fee_sol=fee_sol,
            slot=int(transaction.get("slot")) if transaction.get("slot") is not None else None,
            raw_deltas={delta.symbol: delta.amount for delta in normalized},
            ignored_deltas=ignored,
        )

    def parse_swaps(self, transactions: Iterable[Mapping[str, Any]]) -> list[SwapRecord]:
        """Parse all swap-like transactions in chronological order."""
        swaps = [swap for tx in transactions if (swap := self.parse_swap(tx)) is not None]
        swaps.sort(key=lambda swap: (swap.timestamp, swap.slot or 0, swap.signature))
        return swaps

    def _close_lots(
        self,
        token: AssetDelta,
        proceeds: AssetDelta,
        *,
        timestamp: datetime,
        exit_signature: str,
        inventory: dict[str, deque[InventoryLot]],
    ) -> list[ClosedTrade]:
        """Close FIFO lots for the token leg sold in the current swap."""
        closures: list[ClosedTrade] = []
        remaining_qty = token.amount
        proceeds_currency = self._quote_currency(proceeds.mint)
        lots = inventory[token.mint]

        while remaining_qty > MIN_ASSET_DELTA and lots:
            lot = lots[0]
            matched_qty = min(remaining_qty, lot.quantity)
            if matched_qty <= MIN_ASSET_DELTA:
                break

            quantity_ratio = matched_qty / lot.quantity if lot.quantity else 0.0
            cost_allocated = lot.cost_amount * quantity_ratio
            proceeds_allocated = proceeds.amount * (matched_qty / token.amount)

            realized_pnl: Optional[float] = None
            return_pct: Optional[float] = None
            status = "matched"
            if lot.cost_currency == proceeds_currency and cost_allocated > MIN_ASSET_DELTA:
                realized_pnl = proceeds_allocated - cost_allocated
                return_pct = (realized_pnl / cost_allocated) * 100.0
            else:
                status = "cross_quote"

            closures.append(
                ClosedTrade(
                    token_mint=token.mint,
                    token_symbol=token.symbol,
                    quantity=matched_qty,
                    opened_at=lot.acquired_at,
                    closed_at=timestamp,
                    cost_amount=cost_allocated,
                    cost_currency=lot.cost_currency,
                    proceeds_amount=proceeds_allocated,
                    proceeds_currency=proceeds_currency,
                    realized_pnl=realized_pnl,
                    return_pct=return_pct,
                    entry_signature=lot.entry_signature,
                    exit_signature=exit_signature,
                    status=status,
                )
            )

            remaining_qty -= matched_qty
            lot.quantity -= matched_qty
            lot.cost_amount -= cost_allocated
            if lot.quantity <= MIN_ASSET_DELTA:
                lots.popleft()

        if remaining_qty > MIN_ASSET_DELTA:
            closures.append(
                ClosedTrade(
                    token_mint=token.mint,
                    token_symbol=token.symbol,
                    quantity=remaining_qty,
                    opened_at=timestamp,
                    closed_at=timestamp,
                    cost_amount=0.0,
                    cost_currency="unknown",
                    proceeds_amount=proceeds.amount * (remaining_qty / token.amount),
                    proceeds_currency=proceeds_currency,
                    realized_pnl=None,
                    return_pct=None,
                    entry_signature="",
                    exit_signature=exit_signature,
                    status="unmatched_inventory",
                )
            )

        return closures

    def match_closed_trades(
        self, swaps: Sequence[SwapRecord]
    ) -> tuple[list[ClosedTrade], dict[str, deque[InventoryLot]]]:
        """Build FIFO inventory and closed trades from parsed swaps."""
        inventory: dict[str, deque[InventoryLot]] = defaultdict(deque)
        closed_trades: list[ClosedTrade] = []

        for swap in swaps:
            sold = swap.sold
            bought = swap.bought
            sold_is_quote = self._is_quote_asset(sold.mint)
            bought_is_quote = self._is_quote_asset(bought.mint)

            if not sold_is_quote:
                closed_trades.extend(
                    self._close_lots(
                        sold,
                        bought,
                        timestamp=swap.timestamp,
                        exit_signature=swap.signature,
                        inventory=inventory,
                    )
                )

            if not bought_is_quote:
                inventory[bought.mint].append(
                    InventoryLot(
                        quantity=bought.amount,
                        acquired_at=swap.timestamp,
                        cost_amount=sold.amount,
                        cost_mint=sold.mint,
                        cost_currency=self._quote_currency(sold.mint),
                        entry_signature=swap.signature,
                    )
                )

        return closed_trades, inventory

    def _build_token_summaries(
        self,
        closed_trades: Sequence[ClosedTrade],
        inventory: Mapping[str, deque[InventoryLot]],
    ) -> list[TokenTradeSummary]:
        """Aggregate token-level P&L, hold time, and inventory stats."""
        summaries: dict[str, TokenTradeSummary] = {}

        def ensure_summary(mint: str, symbol: str) -> TokenTradeSummary:
            if mint not in summaries:
                summaries[mint] = TokenTradeSummary(mint=mint, symbol=symbol)
            return summaries[mint]

        for trade in closed_trades:
            summary = ensure_summary(trade.token_mint, trade.token_symbol)
            summary.closed_trades += 1
            if trade.entry_signature:
                summary.hold_seconds_total += trade.hold_seconds
                summary.hold_samples += 1

            if trade.realized_pnl is None:
                summary.unresolved_trades += 1
                continue

            summary.comparable_trades += 1
            summary.realized_pnl_by_currency[trade.proceeds_currency] = (
                summary.realized_pnl_by_currency.get(trade.proceeds_currency, 0.0)
                + trade.realized_pnl
            )
            if trade.return_pct is not None:
                summary.return_pct_total += trade.return_pct
                summary.return_pct_samples += 1
                if trade.return_pct > 0:
                    summary.wins += 1
                elif trade.return_pct < 0:
                    summary.losses += 1
                else:
                    summary.flats += 1

        for mint, lots in inventory.items():
            symbol = _symbol_for_mint(mint, self.symbol_overrides)
            summary = ensure_summary(mint, symbol)
            summary.open_quantity = sum(max(lot.quantity, 0.0) for lot in lots)

        return sorted(
            summaries.values(),
            key=lambda summary: (
                -summary.closed_trades,
                -summary.comparable_trades,
                summary.symbol,
            ),
        )

    def analyze_from_transactions(
        self,
        transactions: Sequence[Mapping[str, Any]],
        *,
        fetched_signatures: Optional[int] = None,
    ) -> WalletAnalysis:
        """Analyze a pre-fetched transaction list."""
        swaps = self.parse_swaps(transactions)
        closed_trades, inventory = self.match_closed_trades(swaps)
        summaries = self._build_token_summaries(closed_trades, inventory)
        return WalletAnalysis(
            wallet_address=self.wallet_address,
            fetched_signatures=fetched_signatures if fetched_signatures is not None else len(transactions),
            analyzed_transactions=len(transactions),
            swap_count=len(swaps),
            total_fee_sol=sum(swap.fee_sol for swap in swaps),
            swaps=swaps,
            closed_trades=closed_trades,
            token_summaries=summaries,
        )

    def analyze(self, *, max_signatures: Optional[int] = None) -> WalletAnalysis:
        """Fetch wallet history from RPC and return a completed analysis."""
        signature_infos = self.fetch_signatures(max_signatures=max_signatures)
        transactions = self.fetch_transactions(signature_infos)
        return self.analyze_from_transactions(
            transactions, fetched_signatures=len(signature_infos)
        )


def render_trade_journal(analysis: WalletAnalysis) -> str:
    """Render a markdown trade journal from analyzed wallet history."""
    generated_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    lines = [
        "# Trade Journal",
        "",
        f"- Generated: {generated_at}",
        f"- Wallet: {_redact_address(analysis.wallet_address)}",
        f"- Signatures fetched: {analysis.fetched_signatures}",
        f"- Transactions analyzed: {analysis.analyzed_transactions}",
        f"- Swaps parsed: {analysis.swap_count}",
        f"- Closed trades: {len(analysis.closed_trades)}",
        f"- Comparable closures: {len(analysis.comparable_closed_trades)}",
        f"- Unresolved closures: {len(analysis.unresolved_closed_trades)}",
        f"- Network fees: {analysis.total_fee_sol:.6f} SOL",
        "",
    ]

    if not analysis.swaps:
        lines.extend(
            [
                "No swap-like transactions were detected for this wallet history.",
                "",
                "## Lessons Learned",
                "",
                "- There is no swap ledger to analyze yet. Fund the wallet or point the analyzer at a wallet with finalized swap history.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## Token Scorecard",
            "",
            "| Token | Closed | Realized P&L | Avg Hold | Wins | Losses | Open Qty | Notes |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for summary in analysis.token_summaries:
        notes: list[str] = []
        if summary.unresolved_trades:
            notes.append(f"{summary.unresolved_trades} unresolved")
        if summary.comparable_trades == 0 and summary.closed_trades:
            notes.append("no comparable exits")
        lines.append(
            "| {token} | {closed} | {pnl} | {hold} | {wins} | {losses} | {open_qty:.6f} | {notes} |".format(
                token=summary.symbol,
                closed=summary.closed_trades,
                pnl=_format_pnl_map(summary.realized_pnl_by_currency),
                hold=_format_timedelta(summary.average_hold_seconds),
                wins=summary.wins,
                losses=summary.losses,
                open_qty=summary.open_quantity,
                notes=", ".join(notes) if notes else "-",
            )
        )

    best_trade = analysis.best_trade
    worst_trade = analysis.worst_trade
    lines.extend(["", "## Best/Worst Trades", ""])
    if best_trade is None or worst_trade is None:
        lines.append("- Not enough comparable round trips to rank best and worst trades.")
    else:
        lines.extend(
            [
                "- Best trade: {token} | {pnl} | {ret:+.2f}% | hold {hold} | {opened} -> {closed}".format(
                    token=best_trade.token_symbol,
                    pnl=_format_quote_amount(
                        float(best_trade.realized_pnl or 0.0),
                        best_trade.proceeds_currency,
                    ),
                    ret=float(best_trade.return_pct or 0.0),
                    hold=_format_timedelta(best_trade.hold_seconds),
                    opened=best_trade.opened_at.isoformat(timespec="seconds"),
                    closed=best_trade.closed_at.isoformat(timespec="seconds"),
                ),
                "- Worst trade: {token} | {pnl} | {ret:+.2f}% | hold {hold} | {opened} -> {closed}".format(
                    token=worst_trade.token_symbol,
                    pnl=_format_quote_amount(
                        float(worst_trade.realized_pnl or 0.0),
                        worst_trade.proceeds_currency,
                    ),
                    ret=float(worst_trade.return_pct or 0.0),
                    hold=_format_timedelta(worst_trade.hold_seconds),
                    opened=worst_trade.opened_at.isoformat(timespec="seconds"),
                    closed=worst_trade.closed_at.isoformat(timespec="seconds"),
                ),
            ]
        )

    lessons: list[str] = []
    comparable = analysis.comparable_closed_trades
    if comparable:
        wins = sum(1 for trade in comparable if float(trade.return_pct or 0.0) > 0)
        win_rate = (wins / len(comparable)) * 100.0
        lessons.append(
            f"Comparable round trips won {win_rate:.1f}% of the time ({wins}/{len(comparable)})."
        )

        winner_holds = [
            trade.hold_seconds
            for trade in comparable
            if float(trade.return_pct or 0.0) > 0
        ]
        loser_holds = [
            trade.hold_seconds
            for trade in comparable
            if float(trade.return_pct or 0.0) < 0
        ]
        if winner_holds and loser_holds:
            avg_winner_hold = sum(winner_holds) / len(winner_holds)
            avg_loser_hold = sum(loser_holds) / len(loser_holds)
            if avg_loser_hold > avg_winner_hold * 1.25:
                lessons.append(
                    "Losses were held longer than winners "
                    f"({_format_timedelta(avg_loser_hold)} vs {_format_timedelta(avg_winner_hold)})."
                )
            elif avg_winner_hold > avg_loser_hold * 1.25:
                lessons.append(
                    "Winners needed more time to work than losers "
                    f"({_format_timedelta(avg_winner_hold)} vs {_format_timedelta(avg_loser_hold)})."
                )

        worst_token = None
        worst_currency = None
        worst_value = 0.0
        for summary in analysis.token_summaries:
            for currency, value in summary.realized_pnl_by_currency.items():
                if worst_token is None or value < worst_value:
                    worst_token = summary.symbol
                    worst_currency = currency
                    worst_value = value
        if worst_token is not None and worst_value < 0:
            lessons.append(
                f"The biggest realized drag came from {worst_token}: "
                f"{_format_quote_amount(worst_value, str(worst_currency))}."
            )
    else:
        lessons.append(
            "No directly comparable round trips were found, so realized P&L is incomplete without an external price series."
        )

    if analysis.unresolved_closed_trades:
        lessons.append(
            f"{len(analysis.unresolved_closed_trades)} closures crossed quote assets or started from pre-history inventory, "
            "so they were flagged instead of force-converted."
        )

    average_fee = analysis.total_fee_sol / analysis.swap_count if analysis.swap_count else 0.0
    lessons.append(
        f"Network fees consumed {analysis.total_fee_sol:.6f} SOL across {analysis.swap_count} swaps "
        f"({average_fee:.6f} SOL per swap)."
    )

    open_positions = [summary for summary in analysis.token_summaries if summary.open_quantity > 0]
    if open_positions:
        total_open = len(open_positions)
        lessons.append(
            f"{total_open} tokens still have open inventory, so realized results exclude unsold bags."
        )

    lines.extend(["", "## Lessons Learned", ""])
    for lesson in lessons:
        lines.append(f"- {lesson}")

    lines.append("")
    return "\n".join(lines)


def write_trade_journal(report: str, output_path: Path | str) -> Path:
    """Persist the rendered markdown journal to disk."""
    return atomic_write_text(output_path, report, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for wallet history analysis."""
    parser = argparse.ArgumentParser(
        description="Analyze a wallet's Solana swap history and write trade_journal.md."
    )
    parser.add_argument(
        "--wallet",
        help="Wallet public key. Falls back to SOLANA_PUBLIC_KEY or the configured keypair.",
    )
    parser.add_argument(
        "--max-signatures",
        type=int,
        default=None,
        help="Optional cap on signature history to analyze.",
    )
    parser.add_argument(
        "--journal-path",
        type=Path,
        default=PROJECT_DIR / "trade_journal.md",
        help="Markdown output path for the generated journal.",
    )
    parser.add_argument(
        "--rpc-url",
        default=RPC_URL,
        help="Solana RPC endpoint to query.",
    )
    args = parser.parse_args(argv)

    try:
        wallet_address = args.wallet or get_pubkey()
    except Exception as exc:
        parser.exit(2, f"wallet configuration error: {exc}\n")

    analyzer = WalletAnalyzer(wallet_address, rpc_url=args.rpc_url)
    analysis = analyzer.analyze(max_signatures=args.max_signatures)
    report = render_trade_journal(analysis)
    output_path = write_trade_journal(report, args.journal_path)
    print(f"Wrote trade journal to {output_path}")
    return 0


__all__ = [
    "AssetDelta",
    "ClosedTrade",
    "SwapRecord",
    "TokenTradeSummary",
    "WalletAnalysis",
    "WalletAnalyzer",
    "render_trade_journal",
    "write_trade_journal",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
