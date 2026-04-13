import pytest
from src.defi.liquidity import Pool, LiquidityPosition, LiquidityManager

def test_impermanent_loss_calculation():
    pool = Pool("SOL-USDC", "Orca", "SOL", "USDC", 0.003)
    # Initial: 10 SOL @ $100 = $1000, 1000 USDC @ $1 = $1000. Total = $2000
    pos = LiquidityPosition(pool, 100.0, 1.0, 10.0, 1000.0)
    
    # Price changes: SOL goes to $400 (4x)
    metrics = pos.calculate_current_value(400.0, 1.0)
    
    # price ratio = 4x
    # IL percentage should be 2*sqrt(4)/(1+4) - 1 = 4/5 - 1 = -0.2 (-20%)
    assert abs(metrics["impermanent_loss_pct"] - (-20.0)) < 0.1
    
    # hold value = 10 * 400 + 1000 * 1 = $5000
    assert metrics["hold_value_usd"] == 5000.0
    
    # k = 10 * 1000 = 10000
    # new price ratio = 400/1 = 400
    # current_amount_a (SOL) = sqrt(10000 / 400) = sqrt(25) = 5
    # current_amount_b (USDC) = sqrt(10000 * 400) = sqrt(4000000) = 2000
    # LP value = 5 * 400 + 2000 * 1 = 4000
    assert metrics["lp_value_usd"] == 4000.0
    
    # IL in USD = LP value - hold value = 4000 - 5000 = -1000
    assert metrics["impermanent_loss_usd"] == -1000.0

def test_profitability_analysis():
    pool = Pool("SOL-USDC", "Raydium", "SOL", "USDC", 0.0025)
    pos = LiquidityPosition(pool, 100.0, 1.0, 10.0, 1000.0)
    
    # Add $1500 in fees
    pos.add_fees(1500.0)
    
    # Analysis when SOL goes to $400
    analysis = pos.analyze_exit_profitability(400.0, 1.0, estimated_gas_fees=1.0)
    
    # IL = -$1000, Fees = $1500, Gas = $1 -> Net vs hold = $499
    assert analysis["lp_vs_hold_profit_usd"] == 499.0
    assert analysis["recommendation"] == "EXIT_PROFITABLE"

def test_liquidity_manager():
    manager = LiquidityManager()
    pool = Pool("RAY-USDC", "Raydium", "RAY", "USDC", 0.0025)
    
    pos = manager.enter_pool("pos1", pool, 2.0, 1.0, 1000.0, 2000.0)
    assert pos.initial_value == 4000.0
    
    report = manager.generate_analysis_report("pos1", 2.0, 1.0)
    assert "RAY-USDC" in report
    assert "Impermanent Loss:   $0.00" in report
