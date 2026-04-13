"""
Jupiter Sentinel - Dollar-Cost Averaging Bot
Schedules regular buys using Jupiter swap.
"""
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from .config import JUPITER_SWAP_V1, HEADERS, SOL_MINT, USDC_MINT
from .validation import build_jupiter_quote_url


@dataclass
class DCAEntry:
    timestamp: float
    amount_sol: float
    price: float
    token_received: float
    tx_signature: Optional[str] = None


@dataclass
class DCAState:
    pair: str
    total_invested_sol: float = 0.0
    total_token_received: float = 0.0
    entries: List[DCAEntry] = field(default_factory=list)
    average_entry_price: float = 0.0
    current_value_sol: float = 0.0
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0

    def update_stats(self) -> None:
        if self.entries:
            self.total_invested_sol = sum(e.amount_sol for e in self.entries)
            self.total_token_received = sum(e.token_received for e in self.entries)
            if self.total_token_received > 0:
                self.average_entry_price = self.total_invested_sol / self.total_token_received


class DCABot:
    """Dollar-cost averaging bot using Jupiter swaps."""

    def __init__(self, amount_per_buy_sol: float = 0.001, interval_seconds: int = 3600) -> None:
        self.amount_per_buy = amount_per_buy_sol
        self.interval = interval_seconds
        self.positions: dict[str, DCAState] = {}

    def get_quote(self, input_mint: str, output_mint: str, amount_lamports: int) -> Optional[dict]:
        try:
            url = build_jupiter_quote_url(
                JUPITER_SWAP_V1,
                input_mint,
                output_mint,
                amount_lamports,
                50,
            )
            req = urllib.request.Request(url, headers=HEADERS)
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        except:
            return None

    def simulate_dca(self, input_mint: str, output_mint: str,
                     num_buys: int = 10, amount_sol: float = 0.001) -> DCAState:
        """Simulate DCA by getting multiple quotes at current price."""
        pair = f"{input_mint[:8]}.../{output_mint[:8]}..."
        state = DCAState(pair=pair)

        for i in range(num_buys):
            lamports = int(amount_sol * 1e9)
            quote = self.get_quote(input_mint, output_mint, lamports)
            if quote:
                out_amount = int(quote.get("outAmount", 0))
                if output_mint == USDC_MINT:
                    token_received = out_amount / 1e6
                    price = amount_sol / token_received if token_received > 0 else 0
                else:
                    token_received = out_amount / 1e9
                    price = amount_sol / token_received if token_received > 0 else 0

                entry = DCAEntry(
                    timestamp=time.time() + i * self.interval,
                    amount_sol=amount_sol,
                    price=price,
                    token_received=token_received,
                )
                state.entries.append(entry)

        state.update_stats()
        return state

    def dca_summary(self, state: DCAState) -> str:
        lines = [
            f"DCA Report: {state.pair}",
            f"Entries: {len(state.entries)}",
            f"Total invested: {state.total_invested_sol:.6f} SOL",
            f"Token received: {state.total_token_received:.6f}",
            f"Avg entry price: {state.average_entry_price:.6f} SOL/token",
        ]
        return "\n".join(lines)
