import time
import json
import urllib.request
from typing import Any, Dict, List, Optional

class SentimentAnalyzer:
    """
    Fetches crypto market sentiment indicators like the Fear & Greed Index
    and trending tokens from CoinGecko. Caches results to avoid rate limits.
    """
    def __init__(self) -> None:
        self.fng_cache: Optional[Dict[str, Any]] = None
        self.fng_last_fetch = 0
        self.trending_cache: Optional[List[Dict[str, Any]]] = None
        self.trending_last_fetch = 0
        self.cache_ttl = 300  # 5 minutes cache

    def get_fear_and_greed(self) -> Dict[str, Any]:
        """Fetch Crypto Fear & Greed Index from alternative.me"""
        now = time.time()
        if self.fng_cache and (now - self.fng_last_fetch) < self.cache_ttl:
            return self.fng_cache

        try:
            req = urllib.request.Request(
                "https://api.alternative.me/fng/",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            data = resp.get("data", [])[0]
            
            self.fng_cache = {
                "value": int(data["value"]),
                "classification": data["value_classification"]
            }
            self.fng_last_fetch = now
            return self.fng_cache
        except Exception as e:
            # Fallback in case of error
            return {"value": 50, "classification": "Neutral"}

    def get_trending_tokens(self) -> List[Dict[str, Any]]:
        """Fetch trending tokens from CoinGecko"""
        now = time.time()
        if self.trending_cache and (now - self.trending_last_fetch) < self.cache_ttl:
            return self.trending_cache

        try:
            req = urllib.request.Request(
                "https://api.coingecko.com/api/v3/search/trending",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            coins = resp.get("coins", [])
            
            trending = []
            for coin in coins[:5]:  # Top 5
                item = coin.get("item", {})
                trending.append({
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "market_cap_rank": item.get("market_cap_rank")
                })
                
            self.trending_cache = trending
            self.trending_last_fetch = now
            return self.trending_cache
        except Exception as e:
            return []
            
    def is_extreme_fear(self) -> bool:
        """Helper to determine if the market is in extreme fear."""
        fng = self.get_fear_and_greed()
        return fng["value"] <= 25
