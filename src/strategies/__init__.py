"""Module explaining what this file does."""

import logging
from typing import Any
from .arbitrage import (
    TriangleEvaluation,
    TriangleQuote,
    TriangularArbitrageScanner,
    scan_for_opportunities as scan_arbitrage_opportunities,
)
from .mean_reversion import scan_for_signals
from .momentum import momentum_score, scan_for_signals as scan_momentum_signals
from .smart_dca import SmartDCAEntry, SmartDCAState, simulate_smart_dca

__all__ = [
    "scan_arbitrage_opportunities",
    "scan_for_signals",
    "scan_momentum_signals",
    "momentum_score",
    "simulate_smart_dca",
    "SmartDCAEntry",
    "SmartDCAState",
    "TriangleEvaluation",
    "TriangleQuote",
    "TriangularArbitrageScanner",
]
