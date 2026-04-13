import os
import json
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path

# Try importing rich for terminal table, fallback to basic print if not available
try:
    from rich.console import Console
    from rich.table import Table
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

class ProfitReport:
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.reports_dir = self.data_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        
    def generate_report(self, 
                        starting_balance: float, 
                        current_balance: float, 
                        total_locked_profit: float,
                        trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Generate a daily profit/loss report.
        
        trades expected format:
        [
            {"id": "...", "profit": 15.5, "symbol": "SOL/USDC", "timestamp": "..."},
            ...
        ]
        """
        today = datetime.now()
        
        # Calculate metrics
        trades_today = len(trades)
        
        winning_trades = [t for t in trades if t.get("profit", 0) > 0]
        losing_trades = [t for t in trades if t.get("profit", 0) <= 0]
        
        win_rate = (len(winning_trades) / trades_today * 100) if trades_today > 0 else 0.0
        
        best_trade = max(trades, key=lambda t: t.get("profit", 0)) if trades else None
        worst_trade = min(trades, key=lambda t: t.get("profit", 0)) if trades else None
        
        best_trade_val = best_trade.get("profit", 0) if best_trade else 0.0
        worst_trade_val = worst_trade.get("profit", 0) if worst_trade else 0.0
        
        # Simple Sharpe ratio estimate (daily returns based on individual trades, annualized)
        # Assuming risk-free rate is 0 for simplicity in crypto intra-day
        profits = [t.get("profit", 0) for t in trades]
        if len(profits) > 1:
            mean_profit = sum(profits) / len(profits)
            variance = sum((p - mean_profit) ** 2 for p in profits) / (len(profits) - 1)
            std_dev = variance ** 0.5
            # Very rough estimate
            sharpe_ratio = (mean_profit / std_dev) * (365 ** 0.5) if std_dev > 0 else 0.0
        else:
            sharpe_ratio = 0.0
            
        total_pnl = current_balance - starting_balance
        pnl_percent = (total_pnl / starting_balance * 100) if starting_balance > 0 else 0.0
        
        report_data = {
            "date": today.strftime("%Y-%m-%d"),
            "time": today.strftime("%H:%M:%S"),
            "starting_balance": starting_balance,
            "current_balance": current_balance,
            "total_pnl": total_pnl,
            "pnl_percent": pnl_percent,
            "total_locked_profit": total_locked_profit,
            "trades_today": trades_today,
            "win_rate": win_rate,
            "best_trade": best_trade_val,
            "worst_trade": worst_trade_val,
            "sharpe_ratio_estimate": sharpe_ratio
        }
        
        self._print_terminal_report(report_data)
        self._save_markdown_report(report_data)
        
        return report_data
        
    def _print_terminal_report(self, data: Dict[str, Any]) -> None:
        if RICH_AVAILABLE:
            console = Console()
            table = Table(title=f"🚀 Daily Profit Report - {data['date']}")
            
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="magenta")
            
            table.add_row("Starting Balance", f"${data['starting_balance']:.2f}")
            table.add_row("Current Balance", f"${data['current_balance']:.2f}")
            
            pnl_color = "green" if data['total_pnl'] >= 0 else "red"
            table.add_row("Total PnL", f"[{pnl_color}]${data['total_pnl']:.2f} ({data['pnl_percent']:.2f}%)[/{pnl_color}]")
            
            table.add_row("Locked Profit", f"${data['total_locked_profit']:.2f}")
            table.add_row("Trades Today", str(data['trades_today']))
            table.add_row("Win Rate", f"{data['win_rate']:.1f}%")
            table.add_row("Best Trade", f"[green]+${data['best_trade']:.2f}[/green]")
            table.add_row("Worst Trade", f"[red]${data['worst_trade']:.2f}[/red]")
            table.add_row("Sharpe Ratio (Est)", f"{data['sharpe_ratio_estimate']:.2f}")
            
            console.print(table)
        else:
            print("=" * 40)
            print(f"🚀 DAILY PROFIT REPORT - {data['date']}")
            print("=" * 40)
            print(f"Starting Balance: ${data['starting_balance']:.2f}")
            print(f"Current Balance:  ${data['current_balance']:.2f}")
            print(f"Total PnL:        ${data['total_pnl']:.2f} ({data['pnl_percent']:.2f}%)")
            print(f"Locked Profit:    ${data['total_locked_profit']:.2f}")
            print(f"Trades Today:     {data['trades_today']}")
            print(f"Win Rate:         {data['win_rate']:.1f}%")
            print(f"Best Trade:       +${data['best_trade']:.2f}")
            print(f"Worst Trade:      ${data['worst_trade']:.2f}")
            print(f"Sharpe Ratio:     {data['sharpe_ratio_estimate']:.2f}")
            print("=" * 40)
            
    def _save_markdown_report(self, data: Dict[str, Any]) -> None:
        filename = f"report_{data['date']}.md"
        filepath = self.reports_dir / filename
        
        md_content = f"""# Daily Profit Report - {data['date']}

Generated at: {data['time']}

## Summary
| Metric | Value |
|--------|-------|
| Starting Balance | ${data['starting_balance']:.2f} |
| Current Balance | ${data['current_balance']:.2f} |
| Total PnL | ${data['total_pnl']:.2f} ({data['pnl_percent']:.2f}%) |
| Locked Profit | ${data['total_locked_profit']:.2f} |

## Trading Metrics
| Metric | Value |
|--------|-------|
| Trades Today | {data['trades_today']} |
| Win Rate | {data['win_rate']:.1f}% |
| Best Trade | +${data['best_trade']:.2f} |
| Worst Trade | ${data['worst_trade']:.2f} |
| Sharpe Ratio (Est) | {data['sharpe_ratio_estimate']:.2f} |

---
*Jupiter Sentinel Automated Report*
"""
        
        with open(filepath, "w") as f:
            f.write(md_content)
            
        print(f"\\n📝 Markdown report saved to: {filepath}")

if __name__ == "__main__":
    # Example usage
    report = ProfitReport()
    
    sample_trades = [
        {"id": "1", "profit": 15.5, "symbol": "SOL/USDC"},
        {"id": "2", "profit": -5.2, "symbol": "JUP/USDC"},
        {"id": "3", "profit": 42.0, "symbol": "WIF/USDC"},
        {"id": "4", "profit": -1.1, "symbol": "BONK/USDC"},
        {"id": "5", "profit": 8.3, "symbol": "SOL/USDC"},
    ]
    
    report.generate_report(
        starting_balance=1000.0,
        current_balance=1059.5,
        total_locked_profit=50.0,
        trades=sample_trades
    )
