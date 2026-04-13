"""
Jupiter Sentinel - Risk Manager
Manages position sizing, stop-losses, and trailing stops
using real-time Jupiter price data.
"""
import math
import time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

from .config import (
    MAX_POSITION_USD, STOP_LOSS_BPS, TAKE_PROFIT_BPS,
    SOL_MINT, USDC_MINT, JUP_MINT, BONK_MINT,
)
from .oracle import PriceFeed
from .executor import TradeExecutor
from .profit_locker import get_tradable_balance
from .security import sanitize_sensitive_text


@dataclass
class Position:
    """An open trading position."""
    pair: str
    input_mint: str
    output_mint: str
    entry_price: float
    amount_sol: float
    entry_time: float
    stop_loss_pct: float = STOP_LOSS_BPS / 10000
    take_profit_pct: float = TAKE_PROFIT_BPS / 10000
    trailing_stop_pct: float = 0.03  # 3% trailing
    highest_price: float = 0.0
    status: str = "open"
    notional: float = 0.0
    tx_buy: Optional[str] = None


class RiskManager:
    """
    Manages risk across positions using Jupiter price feeds.
    
    Features:
    - Position sizing based on volatility
    - Trailing stop-losses
    - Take-profit orders
    - Maximum position limits
    """
    
    def __init__(self, executor: TradeExecutor) -> None:
        self.executor = executor
        self.positions: List[Position] = []
        self.closed_positions: List[dict[str, Any]] = []
        self.price_feeds: Dict[str, PriceFeed] = {}
    
    def open_position(
        self,
        pair: str,
        input_mint: str,
        output_mint: str,
        amount_sol: float,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        dry_run: bool = True,
    ) -> Optional[Position]:
        """
        Open a new position with risk parameters.
        
        Calculates max position size based on current balance
        and volatility-adjusted risk limits.
        """
        if not math.isfinite(amount_sol) or amount_sol <= 0:
            raise ValueError("amount_sol must be a positive finite number")
        if stop_loss_pct is not None and (not math.isfinite(stop_loss_pct) or stop_loss_pct <= 0 or stop_loss_pct >= 1):
            raise ValueError("stop_loss_pct must be a finite decimal between 0 and 1")
        if take_profit_pct is not None and (
            not math.isfinite(take_profit_pct) or take_profit_pct <= 0 or take_profit_pct >= 1
        ):
            raise ValueError("take_profit_pct must be a finite decimal between 0 and 1")

        # Check balance
        balance = self.executor.get_balance()
        if balance["sol_price"] <= 0:
            print("Could not determine SOL price for position sizing")
            return None
        tradable_sol = get_tradable_balance(
            balance.get("sol", 0.0),
            path=getattr(self, "state_path", None),
        )
        max_sol = min(
            amount_sol,
            MAX_POSITION_USD / balance["sol_price"],
            tradable_sol * 0.8,  # Never risk more than 80% of the tradable balance
        )
        position_notional = max_sol * balance["sol_price"]
        if position_notional > MAX_POSITION_USD + 1e-9:
            max_sol = MAX_POSITION_USD / balance["sol_price"]
            position_notional = max_sol * balance["sol_price"]
        
        if max_sol < 0.001:
            print("Insufficient balance for position")
            return None
        
        # Get entry price
        feed = PriceFeed(pair_name=pair, input_mint=input_mint, output_mint=output_mint)
        point = feed.fetch_price()
        if not point:
            print("Could not get entry price")
            return None
        
        position = Position(
            pair=pair,
            input_mint=input_mint,
            output_mint=output_mint,
            entry_price=point.price,
            amount_sol=max_sol,
            entry_time=time.time(),
            stop_loss_pct=stop_loss_pct or (STOP_LOSS_BPS / 10000),
            take_profit_pct=take_profit_pct or (TAKE_PROFIT_BPS / 10000),
            highest_price=point.price,
            notional=position_notional,
        )
        
        if not dry_run:
            # Execute the buy
            lamports = int(max_sol * 1e9)
            result = self.executor.execute_swap(
                input_mint=SOL_MINT,
                output_mint=output_mint,
                amount=lamports,
                dry_run=False,
            )
            if result["status"] != "success":
                print(f"Trade failed: {sanitize_sensitive_text(result.get('error', 'unknown'))}")
                return None
            position.tx_buy = result.get("tx_signature")
        
        self.positions.append(position)
        self.price_feeds[pair] = feed
        
        print(f"[OPEN] {pair} | {max_sol:.6f} SOL @ ${point.price:.4f}")
        print(f"  SL: -{position.stop_loss_pct*100:.1f}% | TP: +{position.take_profit_pct*100:.1f}%")
        
        return position
    
    def check_positions(self) -> List[dict]:
        """
        Check all open positions against stop-loss and take-profit levels.
        Returns list of actions taken.
        """
        actions = []
        
        for pos in self.positions[:]:
            if pos.status != "open":
                continue
            
            feed = self.price_feeds.get(pos.pair)
            if not feed:
                continue
            
            point = feed.fetch_price()
            if not point:
                continue
            
            current_price = point.price
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
            
            # Update trailing stop
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            
            trailing_stop_price = pos.highest_price * (1 - pos.trailing_stop_pct)
            
            action = None
            
            # Check stop loss
            if pnl_pct <= -pos.stop_loss_pct:
                action = {
                    "type": "STOP_LOSS",
                    "pair": pos.pair,
                    "pnl_pct": pnl_pct * 100,
                    "price": current_price,
                }
            
            # Check take profit
            elif pnl_pct >= pos.take_profit_pct:
                action = {
                    "type": "TAKE_PROFIT",
                    "pair": pos.pair,
                    "pnl_pct": pnl_pct * 100,
                    "price": current_price,
                }
            
            # Check trailing stop
            elif current_price <= trailing_stop_price and pos.highest_price > pos.entry_price * 1.01:
                action = {
                    "type": "TRAILING_STOP",
                    "pair": pos.pair,
                    "pnl_pct": pnl_pct * 100,
                    "price": current_price,
                    "highest": pos.highest_price,
                }
            
            if action:
                print(
                    f"[{action['type']}] {pos.pair} | "
                    f"PnL: {pnl_pct*100:+.2f}% @ ${current_price:.4f}"
                )
                pos.status = "closed"
                self.positions.remove(pos)
                self.closed_positions.append({
                    "position": pos,
                    "action": action,
                    "timestamp": datetime.utcnow().isoformat(),
                })
                actions.append(action)
        
        return actions
    
    def get_portfolio_report(self) -> dict[str, Any]:
        """Generate portfolio status report."""
        balance = self.executor.get_balance()
        
        positions_report = []
        for pos in self.positions:
            feed = self.price_feeds.get(pos.pair)
            current = feed.current_price if feed else pos.entry_price
            pnl = (current - pos.entry_price) / pos.entry_price * 100 if current else 0
            
            positions_report.append({
                "pair": pos.pair,
                "entry": pos.entry_price,
                "current": current,
                "pnl_pct": pnl,
                "amount_sol": pos.amount_sol,
                "status": pos.status,
            })
        
        return {
            "wallet": balance,
            "open_positions": positions_report,
            "total_trades": len(self.executor.trade_history),
            "closed_positions": len(self.closed_positions),
        }


if __name__ == "__main__":
    executor = TradeExecutor()
    rm = RiskManager(executor)
    
    # Show current portfolio
    report = rm.get_portfolio_report()
    print(f"Wallet: {report['wallet']['sol']:.6f} SOL (${report['wallet']['usd_value']:.2f})")
    print(f"Open positions: {len(report['open_positions'])}")
    print(f"Total trades: {report['total_trades']}")
