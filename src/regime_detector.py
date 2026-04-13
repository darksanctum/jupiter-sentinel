"""
Market Regime Detector
Analyzes price feeds to determine the current market regime.
"""
from enum import Enum, auto
from typing import Optional
from .oracle import PriceFeed

class MarketRegime(Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    VOLATILE = "VOLATILE"

class RegimeDetector:
    def __init__(self, fast_window: int = 10, slow_window: int = 30, atr_window: int = 14, volatility_threshold: float = 0.015):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.atr_window = atr_window
        self.volatility_threshold = volatility_threshold
        
    def detect(self, feed: PriceFeed) -> MarketRegime:
        """
        Detect current market regime using SMA crossover and ATR-like volatility measure.
        """
        history = list(feed.history)
        if len(history) < self.slow_window:
            return MarketRegime.SIDEWAYS
            
        prices = [p.price for p in history]
        
        # Calculate SMAs
        fast_sma = sum(prices[-self.fast_window:]) / self.fast_window
        slow_sma = sum(prices[-self.slow_window:]) / self.slow_window
        current_price = prices[-1]
        
        # Calculate proxy ATR (average of absolute price changes)
        tr = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        atr_period = min(len(tr), self.atr_window)
        if atr_period == 0:
            atr = 0.0
        else:
            atr = sum(tr[-atr_period:]) / atr_period
            
        # Normalize ATR by price
        normalized_atr = (atr / current_price) if current_price > 0 else 0
        
        if normalized_atr > self.volatility_threshold:
            return MarketRegime.VOLATILE
            
        # Trend detection
        if fast_sma > slow_sma and current_price > fast_sma:
            return MarketRegime.BULL
        elif fast_sma < slow_sma and current_price < fast_sma:
            return MarketRegime.BEAR
        else:
            return MarketRegime.SIDEWAYS
