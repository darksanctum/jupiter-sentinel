"""
Jupiter Sentinel - Grid Trading Strategy
Implements a grid trading bot using Jupiter swap quotes.
"""
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .config import JUPITER_SWAP_V1, HEADERS, SOL_MINT, USDC_MINT, load_keypair
from .validation import build_jupiter_quote_url


@dataclass
class GridLevel:
    price: float
    amount_sol: float
    side: str  # "buy" or "sell"
    filled: bool = False
    pnl: float = 0.0


@dataclass
class GridState:
    pair: str
    center_price: float
    grid_spacing_pct: float
    levels: List[GridLevel] = field(default_factory=list)
    total_pnl: float = 0.0
    filled_count: int = 0


class GridBot:
    """Grid trading bot using Jupiter quotes for price discovery."""

    def __init__(
        self,
        grid_spacing_pct: float = 1.0,
        num_levels: int = 10,
        amount_per_level_sol: float = 0.001,
    ) -> None:
        self.grid_spacing_pct = grid_spacing_pct
        self.num_levels = num_levels
        self.amount_per_level = amount_per_level_sol
        self.grids: List[GridState] = []

    def get_current_price(self, input_mint: str, output_mint: str) -> Optional[float]:
        """Get current price via Jupiter quote."""
        try:
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                input_mint,
                output_mint,
                1_000_000,
                10,
            )
            req = urllib.request.Request(url, headers=HEADERS)
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            out = int(resp["outAmount"])
            if output_mint == USDC_MINT:
                return out / 1e6 / 0.001
            elif output_mint == SOL_MINT:
                return out / 1e9
            return out / 1e6
        except:
            return None

    def create_grid(self, pair: str, input_mint: str, output_mint: str) -> Optional[GridState]:
        """Create a grid around the current price."""
        price = self.get_current_price(input_mint, output_mint)
        if not price:
            return None

        levels = []
        for i in range(-self.num_levels // 2, self.num_levels // 2 + 1):
            level_price = price * (1 + (i * self.grid_spacing_pct / 100))
            side = "buy" if i < 0 else "sell" if i > 0 else "hold"
            levels.append(GridLevel(
                price=round(level_price, 6),
                amount_sol=self.amount_per_level,
                side=side,
            ))

        grid = GridState(
            pair=pair,
            center_price=price,
            grid_spacing_pct=self.grid_spacing_pct,
            levels=levels,
        )
        self.grids.append(grid)
        return grid

    def check_grid(self, grid: GridState, current_price: float) -> List[dict[str, Any]]:
        """Check which grid levels are triggered by current price."""
        triggered = []
        for level in grid.levels:
            if level.filled:
                continue
            if level.side == "buy" and current_price <= level.price:
                level.filled = True
                grid.filled_count += 1
                triggered.append({"action": "BUY", "price": level.price, "amount": level.amount_sol})
            elif level.side == "sell" and current_price >= level.price:
                level.filled = True
                grid.filled_count += 1
                triggered.append({"action": "SELL", "price": level.price, "amount": level.amount_sol})
        return triggered

    def grid_summary(self, grid: GridState) -> str:
        """Generate grid summary."""
        lines = [
            f"Grid: {grid.pair} @ ${grid.center_price:.2f}",
            f"Spacing: {grid.grid_spacing_pct}% | Levels: {len(grid.levels)} | Filled: {grid.filled_count}",
            f"P&L: ${grid.total_pnl:.4f}",
            "Levels:",
        ]
        for i, level in enumerate(grid.levels):
            marker = "X" if level.filled else " "
            lines.append(f"  [{marker}] {level.side:4s} @ ${level.price:.4f} | {level.amount_sol:.4f} SOL")
        return "\n".join(lines)
