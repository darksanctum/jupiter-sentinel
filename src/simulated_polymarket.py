import logging
import time
import uuid
import random
from typing import Dict, Optional
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

class SimulatedPolymarket:
    """
    A simulation layer for Polymarket.
    Tracks real prediction market odds via web scraping (or public Gamma API)
    and simulates positions locally. When real execution is needed, 
    this simulation layer can be swapped out.
    """
    
    def __init__(self, initial_balance: float = 10000.0):
        self.positions: Dict[str, Dict] = {}
        self.balance: float = initial_balance
        # Polymarket's Gamma API endpoint for public market data
        self.api_base = "https://gamma-api.polymarket.com"

    def get_market_odds(self, condition_id: str) -> Optional[Dict[str, float]]:
        """
        Fetches the current odds for a given market condition.
        Attempts to use the Polymarket Gamma API, falls back to randomized simulation if unavailable.
        """
        try:
            # Attempt to fetch real data from Polymarket Gamma API
            # condition_id can be the market slug or id
            url = f"{self.api_base}/events/{condition_id}"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                markets = data.get("markets", [])
                if markets:
                    market = markets[0]
                    outcomes = market.get("outcomes", ["Yes", "No"])
                    try:
                        # outcomePrices can be a list of strings representing floats
                        outcome_prices_str = market.get("outcomePrices", ["0.5", "0.5"])
                        prices = [float(p) for p in outcome_prices_str]
                        return dict(zip(outcomes, prices))
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse prices from API: {e}. Falling back to simulation.")
            else:
                logger.warning(f"Failed to fetch market {condition_id}, status code: {response.status_code}. Falling back to simulation.")
                
        except requests.RequestException as e:
            logger.error(f"Error fetching market odds: {e}. Falling back to simulation.")

        # Fallback simulation data if API fails or market is not found
        logger.info("Using simulated fallback odds.")
        yes_price = random.uniform(0.1, 0.9)
        return {
            "Yes": round(yes_price, 3),
            "No": round(1.0 - yes_price, 3)
        }

    def buy_position(self, condition_id: str, outcome: str, amount_usdc: float) -> Optional[str]:
        """
        Simulates buying shares for a specific outcome.
        """
        if amount_usdc > self.balance:
            logger.error(f"Insufficient balance. Have {self.balance}, need {amount_usdc}")
            return None

        odds = self.get_market_odds(condition_id)
        if not odds or outcome not in odds:
            logger.error(f"Outcome '{outcome}' not found in market {condition_id}")
            return None

        price = odds[outcome]
        if price <= 0:
            logger.error(f"Invalid price for outcome '{outcome}': {price}")
            return None

        shares_bought = amount_usdc / price
        self.balance -= amount_usdc

        position_id = str(uuid.uuid4())
        
        self.positions[position_id] = {
            "condition_id": condition_id,
            "outcome": outcome,
            "shares": shares_bought,
            "average_price": price,
            "invested_usdc": amount_usdc,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        logger.info(f"Simulated BUY: {shares_bought:.2f} shares of '{outcome}' in {condition_id} at ${price:.3f}")
        return position_id

    def sell_position(self, position_id: str) -> bool:
        """
        Simulates selling an existing position at current market odds.
        """
        if position_id not in self.positions:
            logger.error(f"Position {position_id} not found.")
            return False

        position = self.positions[position_id]
        condition_id = position["condition_id"]
        outcome = position["outcome"]
        shares = position["shares"]

        odds = self.get_market_odds(condition_id)
        if not odds or outcome not in odds:
            logger.error(f"Could not retrieve current odds to sell position {position_id}")
            return False

        current_price = odds[outcome]
        sale_value_usdc = shares * current_price
        
        self.balance += sale_value_usdc
        
        profit_loss = sale_value_usdc - position["invested_usdc"]
        
        logger.info(f"Simulated SELL: {shares:.2f} shares of '{outcome}' at ${current_price:.3f}. PnL: ${profit_loss:.2f}")
        
        del self.positions[position_id]
        return True

    def get_portfolio_value(self) -> float:
        """
        Calculates the total value of the portfolio (cash + current value of positions).
        """
        total_value = self.balance
        for pos_id, pos in self.positions.items():
            odds = self.get_market_odds(pos["condition_id"])
            if odds and pos["outcome"] in odds:
                current_price = odds[pos["outcome"]]
                total_value += pos["shares"] * current_price
            else:
                # If can't fetch current odds, use average price as fallback
                total_value += pos["shares"] * pos["average_price"]
                
        return total_value

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    sim = SimulatedPolymarket()
    logger.info(f"Initial balance: ${sim.balance}")
    
    # Try with a known active market slug or random ID for simulation
    market_slug = "bitcoin-price-at-the-end-of-2024" 
    
    logger.info(f"Fetching odds for {market_slug}...")
    odds = sim.get_market_odds(market_slug)
    logger.info(f"Odds: {odds}")
    
    if odds:
        pos_id = sim.buy_position(market_slug, "Yes", 1000.0)
        logger.info(f"Current portfolio value: ${sim.get_portfolio_value():.2f}")
        
        if pos_id:
            time.sleep(2)  # Simulate some time passing
            sim.sell_position(pos_id)
            
    logger.info(f"Final balance: ${sim.balance:.2f}")
