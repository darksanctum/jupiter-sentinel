"""
Jupiter Sentinel - Portfolio Management
Handles capital allocation, profit locking, and DCA scheduling.
"""

import logging
from typing import Any
from typing import Dict, List, Optional
import time

from .dca import DCABot, DCAState
from .risk import RiskManager


class PortfolioManager:
    """
    Manages higher-level portfolio strategies including:
    - Capital allocation
    - DCA scheduling
    - Profit locking
    """

    def __init__(self, risk_manager: RiskManager, total_capital_usd: float) -> None:
        """Function docstring."""
        self.risk_manager = risk_manager
        self.total_capital_usd = total_capital_usd
        self.dca_bots: Dict[str, DCABot] = {}
        self.dca_schedules: Dict[str, dict] = {}

    def allocate_capital(
        self, assets: List[str], strategy: str = "equal"
    ) -> Dict[str, float]:
        """
        Allocate capital across multiple assets based on strategy.
        Returns a dictionary of asset mints to allocated USD.
        """
        allocation = {}
        if not assets:
            return allocation

        if strategy == "equal":
            share = self.total_capital_usd / len(assets)
            for asset in assets:
                allocation[asset] = share
        else:
            # Default to equal allocation if strategy is unknown
            share = self.total_capital_usd / len(assets)
            for asset in assets:
                allocation[asset] = share

        return allocation

    def schedule_dca(
        self,
        pair: str,
        amount_per_buy_sol: float,
        interval_seconds: int,
        total_buys: int,
    ) -> None:
        """
        Schedule a DCA strategy for a specific pair.
        """
        bot = DCABot(
            amount_per_buy_sol=amount_per_buy_sol, interval_seconds=interval_seconds
        )
        self.dca_bots[pair] = bot
        self.dca_schedules[pair] = {
            "amount_sol": amount_per_buy_sol,
            "interval": interval_seconds,
            "total_buys": total_buys,
            "buys_executed": 0,
            "next_buy_time": time.time(),
            "active": True,
        }
        logging.debug(
            "%s",
            f"[DCA SCHEDULED] {pair} | {total_buys} buys of {amount_per_buy_sol} SOL every {interval_seconds}s",
        )

    def execute_dca_step(
        self, pair: str, input_mint: str, output_mint: str
    ) -> Optional[DCAState]:
        """
        Execute the next scheduled DCA buy if it's time.
        """
        schedule = self.dca_schedules.get(pair)
        if not schedule or not schedule["active"]:
            return None

        current_time = time.time()
        if (
            current_time >= schedule["next_buy_time"]
            and schedule["buys_executed"] < schedule["total_buys"]
        ):
            bot = self.dca_bots[pair]

            # Execute 1 buy step using DCABot's simulation for this step
            # In a real scenario, this would use TradeExecutor directly
            state = bot.simulate_dca(
                input_mint=input_mint,
                output_mint=output_mint,
                num_buys=1,
                amount_sol=schedule["amount_sol"],
            )

            schedule["buys_executed"] += 1
            schedule["next_buy_time"] = current_time + schedule["interval"]

            if schedule["buys_executed"] >= schedule["total_buys"]:
                schedule["active"] = False
                logging.debug("%s", f"[DCA COMPLETED] {pair} DCA schedule finished.")

            return state
        return None

    def check_profit_locks(
        self, lock_threshold_pct: float = 0.10, lock_amount_pct: float = 0.50
    ) -> List[dict]:
        """
        Check open positions for profit locking opportunities.
        If a position reaches lock_threshold_pct (e.g., 10% profit),
        close lock_amount_pct (e.g., 50%) of the position to secure gains.
        """
        actions = []
        for pos in self.risk_manager.positions:
            if pos.status != "open":
                continue

            feed = self.risk_manager.price_feeds.get(pos.pair)
            if not feed:
                continue

            point = feed.fetch_price()
            if not point:
                continue

            current_price = point.price
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price

            # Check if profit meets the threshold for taking partial profit
            if pnl_pct >= lock_threshold_pct:
                locked_amount = pos.amount_sol * lock_amount_pct

                action = {
                    "type": "PROFIT_LOCK",
                    "pair": pos.pair,
                    "pnl_pct": pnl_pct * 100,
                    "price": current_price,
                    "locked_amount_sol": locked_amount,
                }

                # Adjust the position amount representing partial closure
                pos.amount_sol -= locked_amount

                # Optionally, adjust entry price or stop loss after profit locking to guarantee no-loss trade
                pos.stop_loss_pct = 0.0  # Move stop loss to breakeven (pnl_pct = 0 implies current_price = entry_price)

                logging.debug(
                    "%s",
                    f"[PROFIT LOCK] {pos.pair} | Locked {locked_amount:.6f} SOL at {pnl_pct*100:+.2f}% profit. Stop-loss moved to breakeven.",
                )
                actions.append(action)

        return actions
