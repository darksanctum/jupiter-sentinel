import json
from typing import Dict, Any

class PortfolioAggregator:
    def __init__(self):
        # In a real scenario, these would be initialized with actual Web3 providers/clients
        pass

    def get_solana_balances(self) -> Dict[str, float]:
        """
        Simulates fetching Solana balances (SOL + tokens).
        Returns balances as USD values for aggregation.
        """
        return {
            "SOL": 1500.00,
            "JUP": 300.00,
            "RAY": 150.00
        }

    def get_polygon_balances(self) -> Dict[str, float]:
        """
        Simulates fetching Polygon balances (POL + USDC).
        Returns balances as USD values for aggregation.
        """
        return {
            "POL": 450.00,
            "USDC": 1000.00
        }

    def get_ethereum_balances(self) -> Dict[str, float]:
        """
        Simulates fetching Ethereum balances (ETH + tokens).
        Returns balances as USD values for aggregation.
        """
        return {
            "ETH": 3500.00,
            "LINK": 800.00,
            "UNI": 400.00
        }

    def aggregate_portfolio(self) -> Dict[str, Any]:
        """
        Aggregates balances across all tracked chains.
        Calculates total value, per-chain breakdown, and allocation percentages.
        """
        sol_balances = self.get_solana_balances()
        pol_balances = self.get_polygon_balances()
        eth_balances = self.get_ethereum_balances()

        sol_total = sum(sol_balances.values())
        pol_total = sum(pol_balances.values())
        eth_total = sum(eth_balances.values())

        total_portfolio_value = sol_total + pol_total + eth_total

        portfolio = {
            "total_value_usd": round(total_portfolio_value, 2),
            "chains": {
                "Solana": {
                    "total_value_usd": round(sol_total, 2),
                    "allocation_percentage": round((sol_total / total_portfolio_value * 100), 2) if total_portfolio_value > 0 else 0,
                    "assets": sol_balances
                },
                "Polygon": {
                    "total_value_usd": round(pol_total, 2),
                    "allocation_percentage": round((pol_total / total_portfolio_value * 100), 2) if total_portfolio_value > 0 else 0,
                    "assets": pol_balances
                },
                "Ethereum": {
                    "total_value_usd": round(eth_total, 2),
                    "allocation_percentage": round((eth_total / total_portfolio_value * 100), 2) if total_portfolio_value > 0 else 0,
                    "assets": eth_balances
                }
            }
        }
        return portfolio

    def get_portfolio_json(self) -> str:
        """Returns the aggregated portfolio as a JSON string for the dashboard."""
        return json.dumps(self.aggregate_portfolio(), indent=4)

if __name__ == "__main__":
    aggregator = PortfolioAggregator()
    print(aggregator.get_portfolio_json())
