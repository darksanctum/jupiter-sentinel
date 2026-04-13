"""
Liquidity provision module for Orca and Raydium pools on Solana.
Provides tools to calculate impermanent loss, track LP token value,
and analyze profitability of entering or exiting pools.
"""

import math
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

class Pool:
    """Represents a liquidity pool."""
    def __init__(self, name: str, protocol: str, token_a: str, token_b: str, fee_tier: float):
        self.name = name
        self.protocol = protocol
        self.token_a = token_a
        self.token_b = token_b
        self.fee_tier = fee_tier

class LiquidityPosition:
    """Tracks an individual liquidity position and calculates metrics."""
    def __init__(
        self,
        pool: Pool,
        initial_price_a_in_usd: float,
        initial_price_b_in_usd: float,
        initial_amount_a: float,
        initial_amount_b: float
    ):
        self.pool = pool
        self.initial_price_a = initial_price_a_in_usd
        self.initial_price_b = initial_price_b_in_usd
        self.initial_amount_a = initial_amount_a
        self.initial_amount_b = initial_amount_b
        self.initial_value = (initial_amount_a * initial_price_a_in_usd) + (initial_amount_b * initial_price_b_in_usd)
        
        # Track fees
        self.accumulated_fees_usd = 0.0

    def add_fees(self, fee_usd: float):
        """Add accumulated fees to the position."""
        self.accumulated_fees_usd += fee_usd

    def calculate_impermanent_loss(self, current_price_a: float, current_price_b: float) -> float:
        """
        Calculate impermanent loss based on current token prices.
        IL = 2 * sqrt(price_ratio) / (1 + price_ratio) - 1
        Returns percentage as a decimal (e.g. -0.05 for -5% loss).
        """
        initial_ratio = self.initial_price_a / self.initial_price_b
        current_ratio = current_price_a / current_price_b
        
        price_ratio = current_ratio / initial_ratio
        
        if price_ratio <= 0:
            return 0.0
            
        il_percentage = (2 * math.sqrt(price_ratio) / (1 + price_ratio)) - 1
        return il_percentage

    def calculate_current_value(self, current_price_a: float, current_price_b: float) -> Dict[str, Any]:
        """
        Calculate the current value of the LP position including fees and impermanent loss.
        Assumes constant product AMM (x * y = k).
        """
        # Constant product k
        k = self.initial_amount_a * self.initial_amount_b
        
        # Current ratio of prices (Price A / Price B)
        # In a constant product pool, the ratio of amounts y/x equals the ratio of prices P_x/P_y
        current_price_ratio = current_price_a / current_price_b
        
        # Calculate new amounts based on current price ratio
        current_amount_a = math.sqrt(k / current_price_ratio) if current_price_ratio > 0 else 0
        current_amount_b = math.sqrt(k * current_price_ratio) if current_price_ratio > 0 else 0
        
        # Calculate hold value (if tokens were just held in wallet instead of LP)
        hold_value = (self.initial_amount_a * current_price_a) + (self.initial_amount_b * current_price_b)
        
        # Calculate LP value (value of tokens currently in the pool)
        lp_value = (current_amount_a * current_price_a) + (current_amount_b * current_price_b)
        
        # IL in USD
        il_usd = lp_value - hold_value
        il_percentage = self.calculate_impermanent_loss(current_price_a, current_price_b)
        
        # Net value including fees
        net_value = lp_value + self.accumulated_fees_usd
        
        # Profit / Loss vs initial investment
        pnl = net_value - self.initial_value
        pnl_percentage = (pnl / self.initial_value) * 100 if self.initial_value > 0 else 0
        
        return {
            "initial_value_usd": self.initial_value,
            "hold_value_usd": hold_value,
            "lp_value_usd": lp_value,
            "current_amount_a": current_amount_a,
            "current_amount_b": current_amount_b,
            "impermanent_loss_usd": il_usd,
            "impermanent_loss_pct": il_percentage * 100,
            "accumulated_fees_usd": self.accumulated_fees_usd,
            "net_value_usd": net_value,
            "net_pnl_usd": pnl,
            "net_pnl_pct": pnl_percentage,
            "is_profitable": pnl > 0
        }

    def analyze_exit_profitability(
        self, 
        current_price_a: float, 
        current_price_b: float, 
        estimated_gas_fees: float = 0.01
    ) -> Dict[str, Any]:
        """
        Analyze whether exiting the pool is profitable compared to just holding the tokens.
        """
        metrics = self.calculate_current_value(current_price_a, current_price_b)
        
        # Net profit from LPing vs Holding (Includes IL and accumulated fees)
        lp_vs_hold_profit = metrics["accumulated_fees_usd"] + metrics["impermanent_loss_usd"] - estimated_gas_fees
        
        decision = "EXIT_PROFITABLE" if lp_vs_hold_profit > 0 else "HOLD_OR_STAY"
        
        return {
            "lp_vs_hold_profit_usd": lp_vs_hold_profit,
            "fees_earned": metrics["accumulated_fees_usd"],
            "impermanent_loss": metrics["impermanent_loss_usd"],
            "gas_fees": estimated_gas_fees,
            "recommendation": decision,
            "metrics": metrics
        }


class LiquidityManager:
    """Manages multiple liquidity positions and provides analysis reports."""
    def __init__(self):
        self.positions: Dict[str, LiquidityPosition] = {}
        logger.info("Initialized Liquidity Manager for Orca/Raydium pools")

    def enter_pool(
        self,
        position_id: str,
        pool: Pool,
        price_a: float,
        price_b: float,
        amount_a: float,
        amount_b: float
    ) -> LiquidityPosition:
        """
        Record a new liquidity position.
        """
        position = LiquidityPosition(
            pool=pool,
            initial_price_a_in_usd=price_a,
            initial_price_b_in_usd=price_b,
            initial_amount_a=amount_a,
            initial_amount_b=amount_b
        )
        self.positions[position_id] = position
        logger.info(f"Entered pool {pool.name} on {pool.protocol} with position ID {position_id}")
        return position

    def get_position(self, position_id: str) -> Optional[LiquidityPosition]:
        """Retrieve a specific position by ID."""
        return self.positions.get(position_id)

    def generate_analysis_report(self, position_id: str, current_price_a: float, current_price_b: float) -> str:
        """
        Generate a formatted text report of IL vs Fee income for a position.
        """
        position = self.get_position(position_id)
        if not position:
            return f"Position {position_id} not found."
            
        analysis = position.analyze_exit_profitability(current_price_a, current_price_b)
        metrics = analysis["metrics"]
        
        report = f"--- Liquidity Position Report: {position.pool.name} ({position.pool.protocol}) ---\n"
        report += f"Initial Investment: ${metrics['initial_value_usd']:.2f}\n"
        report += f"Current LP Value:   ${metrics['lp_value_usd']:.2f}\n"
        report += f"Value if Held:      ${metrics['hold_value_usd']:.2f}\n"
        report += f"--------------------------------------------------\n"
        report += f"Impermanent Loss:   ${metrics['impermanent_loss_usd']:.2f} ({metrics['impermanent_loss_pct']:.2f}%)\n"
        report += f"Accumulated Fees:   ${metrics['accumulated_fees_usd']:.2f}\n"
        report += f"Net LP vs Hold:     ${analysis['lp_vs_hold_profit_usd']:.2f}\n"
        report += f"Recommendation:     {analysis['recommendation']}\n"
        
        return report
