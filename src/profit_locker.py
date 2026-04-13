"""
Jupiter Sentinel - Profit Locker
Persists a locked SOL balance so realized gains can be reserved
outside of the tradable wallet allocation.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR
from .executor import TradeExecutor

STATE_VERSION = 1
DEFAULT_LOCK_PCT = 0.50
LOCK_PCT_ENV = "PROFIT_LOCK_PCT"
PROFIT_LOCK_PATH = DATA_DIR / "profits.json"


def _resolve_path(path: Optional[Path | str] = None) -> Path:
    resolved = Path(path).expanduser() if path is not None else PROFIT_LOCK_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "locked_balance": 0.0,
        "updated_at": _utcnow(),
    }


def _load_state(path: Optional[Path | str] = None) -> dict[str, Any]:
    state_path = _resolve_path(path)
    if not state_path.exists():
        return _default_state()

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid profit locker file: {state_path}") from exc

    locked_balance = float(payload.get("locked_balance", 0.0) or 0.0)
    if not math.isfinite(locked_balance) or locked_balance < 0:
        raise ValueError(f"Invalid locked_balance in profit locker file: {state_path}")

    return {
        "version": int(payload.get("version", STATE_VERSION) or STATE_VERSION),
        "locked_balance": locked_balance,
        "updated_at": str(payload.get("updated_at", "")),
    }


def _write_state(state: dict[str, Any], path: Optional[Path | str] = None) -> None:
    state_path = _resolve_path(path)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def _resolve_lock_pct(lock_pct: Optional[float] = None) -> float:
    if lock_pct is None:
        raw_value = os.environ.get(LOCK_PCT_ENV, "").strip()
        lock_pct = DEFAULT_LOCK_PCT if not raw_value else float(raw_value)

    if not math.isfinite(lock_pct) or lock_pct < 0 or lock_pct > 1:
        raise ValueError("lock_pct must be a finite decimal between 0 and 1")

    return float(lock_pct)


def lock_profit(amount: float, lock_pct: Optional[float] = None, path: Optional[Path | str] = None) -> float:
    """
    Lock a percentage of realized profit and persist it to disk.

    Returns the amount that was added to the locked balance.
    """
    amount = float(amount)
    if not math.isfinite(amount):
        raise ValueError("amount must be a finite number")
    if amount <= 0:
        return 0.0

    state = _load_state(path)
    locked_amount = amount * _resolve_lock_pct(lock_pct)
    state["locked_balance"] = state["locked_balance"] + locked_amount
    state["updated_at"] = _utcnow()
    _write_state(state, path)
    return locked_amount


def get_locked_balance(path: Optional[Path | str] = None) -> float:
    """Return the total locked SOL balance."""
    return float(_load_state(path)["locked_balance"])


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


__all__ = ["lock_profit", "get_locked_balance", "get_tradable_balance"]
