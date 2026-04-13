"""
Jupiter Sentinel - Grid Trading Strategy
Persistent grid trading built on top of Jupiter execution.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR, HEADERS, JUPITER_SWAP_V1, RPC_URL, SOL_MINT, USDC_MINT, get_pubkey
from .executor import TradeExecutor
from .resilience import (
    archive_corrupt_file,
    read_json_file,
    request_json,
    restore_json_from_backup,
    write_json_state,
)
from .validation import build_jupiter_quote_url

SUCCESS_STATUSES = {"success", "dry_run"}
PRICE_ROUNDING = 12
KNOWN_TOKEN_DECIMALS = {
    SOL_MINT: 9,
    USDC_MINT: 6,
}


@dataclass
class GridLevel:
    """
    One active order slot in the grid.

    `amount_sol` is kept for backwards compatibility with the original module.
    It represents the base-asset size for the level, even when the base asset
    is not SOL.
    """

    price: float
    amount_sol: float
    side: str  # "buy" or "sell"
    filled: bool = False
    pnl: float = 0.0
    reserved_base: float = 0.0
    reserved_quote: float = 0.0
    fill_count: int = 0
    last_status: str = "pending"
    last_error: str = ""
    last_tx_signature: Optional[str] = None
    cost_basis_quote: float = 0.0

    @property
    def funded(self) -> bool:
        return self.reserved_base > 0 or self.reserved_quote > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GridLevel":
        return cls(
            price=float(payload.get("price", 0.0) or 0.0),
            amount_sol=float(payload.get("amount_sol", 0.0) or 0.0),
            side=str(payload.get("side", "buy") or "buy"),
            filled=bool(payload.get("filled", False)),
            pnl=float(payload.get("pnl", 0.0) or 0.0),
            reserved_base=float(payload.get("reserved_base", 0.0) or 0.0),
            reserved_quote=float(payload.get("reserved_quote", 0.0) or 0.0),
            fill_count=int(payload.get("fill_count", 0) or 0),
            last_status=str(payload.get("last_status", "pending") or "pending"),
            last_error=str(payload.get("last_error", "") or ""),
            last_tx_signature=payload.get("last_tx_signature"),
            cost_basis_quote=float(payload.get("cost_basis_quote", 0.0) or 0.0),
        )


@dataclass
class GridState:
    pair: str
    center_price: float
    grid_spacing_pct: float
    levels: list[GridLevel] = field(default_factory=list)
    total_pnl: float = 0.0
    filled_count: int = 0
    input_mint: str = ""
    output_mint: str = ""
    input_decimals: int = 9
    output_decimals: int = 6
    num_levels: int = 0
    range_low: float = 0.0
    range_high: float = 0.0
    last_price: float = 0.0
    rebalance_count: int = 0
    unallocated_base: float = 0.0
    unallocated_quote: float = 0.0
    wallet_address: str = ""
    last_error: str = ""
    status: str = "idle"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["levels"] = [level.to_dict() for level in self.levels]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GridState":
        levels = [GridLevel.from_dict(level) for level in payload.get("levels", [])]
        range_low = float(payload.get("range_low", 0.0) or 0.0)
        range_high = float(payload.get("range_high", 0.0) or 0.0)
        if not range_low and levels:
            range_low = min(level.price for level in levels)
        if not range_high and levels:
            range_high = max(level.price for level in levels)
        num_levels = int(payload.get("num_levels", 0) or 0)
        if num_levels <= 0 and levels:
            num_levels = max(len(levels) // 2, 1)

        return cls(
            pair=str(payload.get("pair", "") or ""),
            center_price=float(payload.get("center_price", 0.0) or 0.0),
            grid_spacing_pct=float(payload.get("grid_spacing_pct", 0.0) or 0.0),
            levels=levels,
            total_pnl=float(payload.get("total_pnl", 0.0) or 0.0),
            filled_count=int(payload.get("filled_count", 0) or 0),
            input_mint=str(payload.get("input_mint", "") or ""),
            output_mint=str(payload.get("output_mint", "") or ""),
            input_decimals=int(payload.get("input_decimals", 9) or 9),
            output_decimals=int(payload.get("output_decimals", 6) or 6),
            num_levels=num_levels,
            range_low=range_low,
            range_high=range_high,
            last_price=float(payload.get("last_price", 0.0) or 0.0),
            rebalance_count=int(payload.get("rebalance_count", 0) or 0),
            unallocated_base=float(payload.get("unallocated_base", 0.0) or 0.0),
            unallocated_quote=float(payload.get("unallocated_quote", 0.0) or 0.0),
            wallet_address=str(payload.get("wallet_address", "") or ""),
            last_error=str(payload.get("last_error", "") or ""),
            status=str(payload.get("status", "idle") or "idle"),
        )


class GridBot:
    """Persistent grid trader that arms buy levels below price and sell levels above."""

    def __init__(
        self,
        grid_spacing_pct: float = 2.0,
        num_levels: int = 10,
        amount_per_level_sol: float = 0.001,
        *,
        executor: Optional[TradeExecutor] = None,
        state_path: Path | str = DATA_DIR / "grid_state.json",
    ) -> None:
        if grid_spacing_pct <= 0:
            raise ValueError("grid_spacing_pct must be positive")
        if num_levels <= 0:
            raise ValueError("num_levels must be positive")
        if amount_per_level_sol <= 0:
            raise ValueError("amount_per_level_sol must be positive")

        self.grid_spacing_pct = float(grid_spacing_pct)
        self.num_levels = int(num_levels)
        self.amount_per_level = float(amount_per_level_sol)
        self.executor = executor or TradeExecutor()
        self.state_path = Path(state_path).expanduser()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        self._grids_by_pair: dict[str, GridState] = {}
        self.grids: list[GridState] = []
        self._decimals_cache = dict(KNOWN_TOKEN_DECIMALS)
        self._load_state()

    def get_grid(self, pair: str) -> Optional[GridState]:
        return self._grids_by_pair.get(pair)

    def get_current_price(self, input_mint: str, output_mint: str) -> Optional[float]:
        """Get the current base/quote price from a small Jupiter quote."""
        try:
            input_decimals = self.get_token_decimals(input_mint)
            output_decimals = self.get_token_decimals(output_mint)
            amount = self._price_probe_amount(input_mint, input_decimals)
            quote = self._get_quote(input_mint, output_mint, amount, slippage_bps=10)
            out_amount = int(quote.get("outAmount", 0) or 0)
            if out_amount <= 0:
                return None

            base_amount = amount / (10 ** input_decimals)
            quote_amount = out_amount / (10 ** output_decimals)
            if base_amount <= 0:
                return None
            return quote_amount / base_amount
        except Exception:
            return None

    def create_grid(
        self,
        pair: str,
        input_mint: str,
        output_mint: str,
        *,
        current_price: Optional[float] = None,
        base_balance: Optional[float] = None,
        quote_balance: Optional[float] = None,
    ) -> Optional[GridState]:
        """Create or replace a grid around the current market price."""
        price = current_price if current_price is not None else self.get_current_price(input_mint, output_mint)
        if price is None or price <= 0:
            return None

        input_decimals = self.get_token_decimals(input_mint)
        output_decimals = self.get_token_decimals(output_mint)
        if base_balance is None:
            base_balance = self.get_wallet_balance(input_mint)
        if quote_balance is None:
            quote_balance = self.get_wallet_balance(output_mint)

        grid = GridState(
            pair=pair,
            center_price=float(price),
            grid_spacing_pct=self.grid_spacing_pct,
            levels=self._build_levels(float(price)),
            input_mint=input_mint,
            output_mint=output_mint,
            input_decimals=input_decimals,
            output_decimals=output_decimals,
            num_levels=self.num_levels,
            last_price=float(price),
            wallet_address=self._get_wallet_address(),
            status="ready",
        )
        self._set_grid_range(grid)
        self._allocate_inventory(grid, float(base_balance or 0.0), float(quote_balance or 0.0))
        self._store_grid(grid)
        self._save_state()
        return grid

    def run_once(
        self,
        pair: str,
        *,
        current_price: Optional[float] = None,
        dry_run: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch price, process triggered levels, and persist the updated grid."""
        grid = self.get_grid(pair)
        if grid is None:
            raise KeyError(f"Grid not found for pair: {pair}")

        observed_price = current_price
        if observed_price is None:
            observed_price = self.get_current_price(grid.input_mint, grid.output_mint)
        if observed_price is None or observed_price <= 0:
            grid.status = "error"
            grid.last_error = "Unable to fetch current price"
            self._save_state()
            return [{
                "pair": grid.pair,
                "status": "error",
                "reason": grid.last_error,
                "price": 0.0,
            }]

        return self.check_grid(grid, float(observed_price), dry_run=dry_run)

    def check_grid(
        self,
        grid: GridState,
        current_price: float,
        *,
        dry_run: bool = True,
    ) -> list[dict[str, Any]]:
        """Execute all grid levels triggered by the latest price update."""
        actions: list[dict[str, Any]] = []
        movement = current_price - (grid.last_price or grid.center_price)
        buy_levels = sorted(
            [level for level in grid.levels if level.side == "buy" and current_price <= level.price],
            key=lambda level: level.price,
            reverse=True,
        )
        sell_levels = sorted(
            [level for level in grid.levels if level.side == "sell" and current_price >= level.price],
            key=lambda level: level.price,
        )

        ordered_levels = sell_levels + buy_levels if movement >= 0 else buy_levels + sell_levels
        for level in ordered_levels:
            actions.append(self._execute_level(grid, level, current_price, dry_run=dry_run))

        if current_price < grid.range_low or current_price > grid.range_high:
            self._recenter_grid(grid, current_price)
            actions.append(
                {
                    "pair": grid.pair,
                    "action": "RECENTER",
                    "price": current_price,
                    "amount": 0.0,
                    "status": "recentered",
                }
            )

        grid.last_price = current_price
        if not actions and grid.status != "recentered":
            grid.status = "idle"
            grid.last_error = ""
        self._save_state()
        return actions

    def grid_summary(self, grid: GridState) -> str:
        """Generate a compact operational summary of the grid."""
        lines = [
            f"Grid: {grid.pair} @ {grid.center_price:.6f}",
            (
                f"Spacing: {grid.grid_spacing_pct:.2f}% | Levels/side: {grid.num_levels} | "
                f"Filled: {grid.filled_count} | Recentered: {grid.rebalance_count}"
            ),
            (
                f"Range: {grid.range_low:.6f} - {grid.range_high:.6f} | "
                f"Unallocated base: {grid.unallocated_base:.6f} | "
                f"Unallocated quote: {grid.unallocated_quote:.6f}"
            ),
            f"P&L (quote): {grid.total_pnl:.6f}",
            "Levels:",
        ]
        for level in sorted(grid.levels, key=lambda item: item.price):
            marker = "X" if level.filled else " "
            if level.side == "buy":
                reserve = f"quote={level.reserved_quote:.6f}"
            else:
                reserve = f"base={level.reserved_base:.6f}"
            lines.append(
                f"  [{marker}] {level.side:4s} @ {level.price:.6f} | "
                f"size={level.amount_sol:.6f} | {reserve} | {level.last_status}"
            )
        return "\n".join(lines)

    def get_wallet_balance(self, mint: str) -> float:
        """Fetch the wallet balance for SOL or an SPL token."""
        try:
            if mint == SOL_MINT:
                balance = self.executor.get_balance()
                return float(balance.get("sol", 0.0) or 0.0)

            wallet = self._get_wallet_address()
            if not wallet:
                return 0.0

            response = self._rpc_request(
                "getTokenAccountsByOwner",
                [
                    wallet,
                    {"mint": mint},
                    {"encoding": "jsonParsed"},
                ],
            )
            total = 0.0
            for account in response.get("value", []):
                parsed = (((account.get("account") or {}).get("data") or {}).get("parsed") or {})
                info = parsed.get("info") or {}
                token_amount = info.get("tokenAmount") or {}
                ui_amount = token_amount.get("uiAmount")
                if ui_amount is not None:
                    total += float(ui_amount)
                    continue

                amount_raw = int(token_amount.get("amount", 0) or 0)
                decimals = int(token_amount.get("decimals", self.get_token_decimals(mint)) or 0)
                total += amount_raw / (10 ** decimals)
            return total
        except Exception:
            return 0.0

    def get_token_decimals(self, mint: str) -> int:
        """Resolve mint decimals, caching on success."""
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        if mint == SOL_MINT:
            self._decimals_cache[mint] = 9
            return 9

        response = self._rpc_request("getTokenSupply", [mint])
        decimals = int(((response.get("value") or {}).get("decimals", 0)) or 0)
        if decimals <= 0:
            raise ValueError(f"Unable to determine token decimals for mint {mint}")
        self._decimals_cache[mint] = decimals
        return decimals

    def _build_levels(self, center_price: float) -> list[GridLevel]:
        spacing_factor = 1 + (self.grid_spacing_pct / 100.0)
        levels: list[GridLevel] = []
        for step in range(1, self.num_levels + 1):
            buy_price = round(center_price / (spacing_factor ** step), PRICE_ROUNDING)
            levels.append(GridLevel(price=buy_price, amount_sol=self.amount_per_level, side="buy"))
        for step in range(1, self.num_levels + 1):
            sell_price = round(center_price * (spacing_factor ** step), PRICE_ROUNDING)
            levels.append(GridLevel(price=sell_price, amount_sol=self.amount_per_level, side="sell"))
        return levels

    def _allocate_inventory(self, grid: GridState, base_balance: float, quote_balance: float) -> None:
        for level in grid.levels:
            level.reserved_base = 0.0
            level.reserved_quote = 0.0
            level.last_tx_signature = None
            level.last_error = ""
            level.last_status = "pending"

        base_remaining = max(float(base_balance), 0.0)
        quote_remaining = max(float(quote_balance), 0.0)

        buy_levels = sorted(
            [level for level in grid.levels if level.side == "buy"],
            key=lambda level: level.price,
            reverse=True,
        )
        sell_levels = sorted(
            [level for level in grid.levels if level.side == "sell"],
            key=lambda level: level.price,
        )

        for level in buy_levels:
            required_quote = level.amount_sol * level.price
            if quote_remaining + 1e-12 >= required_quote:
                level.reserved_quote = required_quote
                level.last_status = "armed"
                quote_remaining -= required_quote
            else:
                level.last_status = "unfunded"
                level.last_error = "insufficient quote balance"

        for level in sell_levels:
            required_base = level.amount_sol
            if base_remaining + 1e-12 >= required_base:
                level.reserved_base = required_base
                level.last_status = "armed"
                base_remaining -= required_base
            else:
                level.last_status = "unfunded"
                level.last_error = "insufficient base balance"

        grid.unallocated_base = base_remaining
        grid.unallocated_quote = quote_remaining
        grid.status = "ready" if any(level.funded for level in grid.levels) else "waiting_for_funds"
        grid.last_error = ""

    def _execute_level(
        self,
        grid: GridState,
        level: GridLevel,
        current_price: float,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        trigger_price = level.price
        action = {
            "pair": grid.pair,
            "action": level.side.upper(),
            "price": trigger_price,
            "current_price": current_price,
            "amount": level.amount_sol,
            "status": "skipped",
        }

        if level.side == "buy":
            quote_in = level.reserved_quote
            if quote_in <= 0:
                level.last_status = "unfunded"
                level.last_error = "insufficient quote balance"
                grid.status = "warning"
                grid.last_error = level.last_error
                action["reason"] = level.last_error
                return action

            amount_units = self._to_units(quote_in, grid.output_decimals)
            if amount_units <= 0:
                level.last_status = "unfunded"
                level.last_error = "quote reserve too small to execute"
                grid.status = "warning"
                grid.last_error = level.last_error
                action["reason"] = level.last_error
                return action

            result = self.executor.execute_swap(
                input_mint=grid.output_mint,
                output_mint=grid.input_mint,
                amount=amount_units,
                dry_run=dry_run,
            )
            action["quote_in"] = quote_in
            if result.get("status") not in SUCCESS_STATUSES:
                level.last_status = str(result.get("status", "failed") or "failed")
                level.last_error = str(result.get("error", "swap failed") or "swap failed")
                grid.status = "warning"
                grid.last_error = level.last_error
                action["status"] = level.last_status
                action["reason"] = level.last_error
                return action

            base_out = self._from_units(int(result.get("out_amount", 0) or 0), grid.input_decimals)
            if base_out <= 0:
                level.last_status = "failed"
                level.last_error = "Jupiter returned zero output amount"
                grid.status = "warning"
                grid.last_error = level.last_error
                action["status"] = "failed"
                action["reason"] = level.last_error
                return action

            level.reserved_quote = 0.0
            level.reserved_base = base_out
            level.cost_basis_quote = quote_in
            level.side = "sell"
            level.price = round(trigger_price * (1 + grid.grid_spacing_pct / 100.0), PRICE_ROUNDING)
            level.fill_count += 1
            level.filled = True
            level.last_status = str(result.get("status", "success") or "success")
            level.last_error = ""
            level.last_tx_signature = result.get("tx_signature")
            grid.filled_count += 1
            grid.status = "executed"
            grid.last_error = ""

            action["status"] = level.last_status
            action["amount"] = base_out
            action["base_out"] = base_out
            if level.last_tx_signature:
                action["tx_signature"] = level.last_tx_signature
            return action

        base_in = level.reserved_base
        if base_in <= 0:
            level.last_status = "unfunded"
            level.last_error = "insufficient base balance"
            grid.status = "warning"
            grid.last_error = level.last_error
            action["reason"] = level.last_error
            return action

        amount_units = self._to_units(base_in, grid.input_decimals)
        if amount_units <= 0:
            level.last_status = "unfunded"
            level.last_error = "base reserve too small to execute"
            grid.status = "warning"
            grid.last_error = level.last_error
            action["reason"] = level.last_error
            return action

        result = self.executor.execute_swap(
            input_mint=grid.input_mint,
            output_mint=grid.output_mint,
            amount=amount_units,
            dry_run=dry_run,
        )
        action["base_in"] = base_in
        if result.get("status") not in SUCCESS_STATUSES:
            level.last_status = str(result.get("status", "failed") or "failed")
            level.last_error = str(result.get("error", "swap failed") or "swap failed")
            grid.status = "warning"
            grid.last_error = level.last_error
            action["status"] = level.last_status
            action["reason"] = level.last_error
            return action

        quote_out = self._from_units(int(result.get("out_amount", 0) or 0), grid.output_decimals)
        if quote_out <= 0:
            level.last_status = "failed"
            level.last_error = "Jupiter returned zero output amount"
            grid.status = "warning"
            grid.last_error = level.last_error
            action["status"] = "failed"
            action["reason"] = level.last_error
            return action

        level.reserved_base = 0.0
        level.reserved_quote = quote_out
        if level.cost_basis_quote > 0:
            realized = quote_out - level.cost_basis_quote
            level.pnl += realized
            grid.total_pnl += realized
        level.cost_basis_quote = 0.0
        level.side = "buy"
        level.price = round(trigger_price / (1 + grid.grid_spacing_pct / 100.0), PRICE_ROUNDING)
        level.fill_count += 1
        level.filled = True
        level.last_status = str(result.get("status", "success") or "success")
        level.last_error = ""
        level.last_tx_signature = result.get("tx_signature")
        grid.filled_count += 1
        grid.status = "executed"
        grid.last_error = ""

        action["status"] = level.last_status
        action["amount"] = quote_out
        action["quote_out"] = quote_out
        if level.last_tx_signature:
            action["tx_signature"] = level.last_tx_signature
        return action

    def _recenter_grid(self, grid: GridState, current_price: float) -> None:
        base_balance, quote_balance = self._reserved_inventory(grid)
        grid.center_price = current_price
        grid.levels = self._build_levels(current_price)
        grid.rebalance_count += 1
        grid.last_error = ""
        self._set_grid_range(grid)
        self._allocate_inventory(grid, base_balance, quote_balance)
        grid.status = "recentered"

    def _reserved_inventory(self, grid: GridState) -> tuple[float, float]:
        base_total = grid.unallocated_base + sum(level.reserved_base for level in grid.levels)
        quote_total = grid.unallocated_quote + sum(level.reserved_quote for level in grid.levels)
        return base_total, quote_total

    def _set_grid_range(self, grid: GridState) -> None:
        prices = [level.price for level in grid.levels]
        grid.range_low = min(prices) if prices else 0.0
        grid.range_high = max(prices) if prices else 0.0

    def _price_probe_amount(self, mint: str, decimals: int) -> int:
        if mint == SOL_MINT:
            return 1_000_000  # 0.001 SOL
        return max(10 ** decimals, 1)

    def _get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        *,
        slippage_bps: int,
    ) -> dict[str, Any]:
        getter = getattr(self.executor, "get_quote", None)
        if callable(getter):
            return getter(input_mint, output_mint, amount, slippage_bps)

        url = build_jupiter_quote_url(
            JUPITER_SWAP_V1,
            input_mint,
            output_mint,
            amount,
            slippage_bps,
        )
        req = urllib.request.Request(url, headers=HEADERS)
        return request_json(req, timeout=15, describe="Jupiter grid quote")

    def _rpc_request(self, method: str, params: list[Any]) -> dict[str, Any]:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }
        ).encode()
        req = urllib.request.Request(
            RPC_URL,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": HEADERS.get("User-Agent", "JupiterSentinel/1.0")},
        )
        response = request_json(req, timeout=15, describe=f"Solana RPC {method}")
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        return response.get("result", {})

    def _get_wallet_address(self) -> str:
        pubkey = str(getattr(self.executor, "pubkey", "") or "").strip()
        if pubkey:
            return pubkey
        try:
            return get_pubkey()
        except Exception:
            return ""

    def _to_units(self, amount: float, decimals: int) -> int:
        units = int(round(max(amount, 0.0) * (10 ** decimals)))
        return max(units, 0)

    def _from_units(self, amount: int, decimals: int) -> float:
        return float(amount) / (10 ** decimals)

    def _store_grid(self, grid: GridState) -> None:
        self._grids_by_pair[grid.pair] = grid
        self.grids = list(self._grids_by_pair.values())

    def _load_state(self) -> None:
        payload = self._read_state_payload()
        raw_grids = payload.get("grids", [])
        if isinstance(raw_grids, dict):
            raw_grids = list(raw_grids.values())

        self._grids_by_pair = {}
        for raw_grid in raw_grids:
            if not isinstance(raw_grid, dict):
                continue
            grid = GridState.from_dict(raw_grid)
            if grid.pair:
                self._grids_by_pair[grid.pair] = grid
        self.grids = list(self._grids_by_pair.values())

    def _read_state_payload(self) -> dict[str, Any]:
        if not self.state_path.exists():
            backup_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")
            if backup_path.exists():
                try:
                    return restore_json_from_backup(
                        self.state_path,
                        backup_path=backup_path,
                        default_factory=dict,
                    )
                except (json.JSONDecodeError, OSError, ValueError):
                    archive_corrupt_file(backup_path)
            return {}

        try:
            return read_json_file(self.state_path)
        except (json.JSONDecodeError, OSError, ValueError):
            backup_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")
            archive_corrupt_file(self.state_path)
            if backup_path.exists():
                try:
                    return restore_json_from_backup(
                        self.state_path,
                        backup_path=backup_path,
                        default_factory=dict,
                    )
                except (json.JSONDecodeError, OSError, ValueError):
                    archive_corrupt_file(backup_path)
            return {}

    def _save_state(self) -> None:
        payload = {
            "grids": [grid.to_dict() for grid in self._grids_by_pair.values()],
        }
        backup_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")
        write_json_state(self.state_path, payload, backup_path=backup_path)
