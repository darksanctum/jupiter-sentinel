"""
Jupiter Sentinel - Profit Locker
Thin compatibility wrapper over the shared bot state manager.
"""

from __future__ import annotations
import logging
from typing import Any

import math
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .executor import TradeExecutor
from .state_manager import DEFAULT_LOCK_PCT, LOCK_PCT_ENV, StateManager

PROFIT_LOCK_PATH = DATA_DIR / "state.json"


def _resolve_manager(path: Optional[Path | str] = None) -> StateManager:
    """Function docstring."""
    return StateManager(path or PROFIT_LOCK_PATH)


def lock_profit(
    amount: float, lock_pct: Optional[float] = None, path: Optional[Path | str] = None
) -> float:
    """
    Lock a percentage of realized profit and persist it to disk.

    Returns the amount that was added to the locked balance.
    """
    return _resolve_manager(path).lock_profit(amount, lock_pct=lock_pct)


def get_locked_balance(path: Optional[Path | str] = None) -> float:
    """Return the total locked SOL balance."""
    return _resolve_manager(path).get_locked_balance()


def get_tradable_balance(
    total_balance: Optional[float] = None,
    *,
    executor: Optional[TradeExecutor] = None,
    path: Optional[Path | str] = None,
) -> float:
    """
    Return the SOL balance still available for trading after locked profits.

    If `total_balance` is omitted, the current wallet SOL balance is fetched
    from the provided executor or a fresh TradeExecutor instance.
    """
    if total_balance is None:
        balance = (executor or TradeExecutor()).get_balance()
        total_balance = float(balance.get("sol", 0.0) or 0.0)
    else:
        total_balance = float(total_balance)

    if not math.isfinite(total_balance):
        raise ValueError("total_balance must be a finite number")

    return max(total_balance - get_locked_balance(path), 0.0)


__all__ = [
    "DEFAULT_LOCK_PCT",
    "LOCK_PCT_ENV",
    "PROFIT_LOCK_PATH",
    "get_locked_balance",
    "get_tradable_balance",
    "lock_profit",
]
