"""
Jupiter Sentinel - State Manager
Persists bot runtime state, profit locks, and crash recovery snapshots.
"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .resilience import (
    archive_corrupt_file,
    read_json_file,
    restore_json_from_backup,
    write_json_state,
)

DEFAULT_LOCK_PCT = 0.5
LOCK_PCT_ENV = "PROFIT_LOCK_PCT"
BOT_CONFIG_FIELDS = (
    "dry_run",
    "cycle",
    "entry_amount_sol",
    "enter_on",
    "max_open_positions",
    "scan_interval_secs",
)


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


class StateManager:
    def __init__(
        self,
        path: Path | str,
        auto_save_interval: float = 5.0,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.backup_path = self.path.with_suffix(f"{self.path.suffix}.bak")
        self.auto_save_interval = float(auto_save_interval)
        self.logger = logger or (lambda message: None)

        self._lock = threading.RLock()
        self._data = self._default_state()
        self._dirty = False
        self._auto_save_thread: Optional[threading.Thread] = None
        self._auto_save_stop = threading.Event()
        self._auto_save_callback: Optional[Callable[[], None]] = None

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": _utcnow(),
            "bot_config": {},
            "positions": {
                "open": [],
                "closed": [],
            },
            "open_positions": [],
            "closed_positions": [],
            "trade_history": [],
            "alerts": [],
            "scanner_feeds": [],
            "profit_tracking": {
                "realized_profit_sol": 0.0,
                "locked_profit_sol": 0.0,
            },
            "locked_balance": 0.0,
        }

    def _normalize(self, snapshot: Optional[dict[str, Any]]) -> dict[str, Any]:
        raw = deepcopy(snapshot or {})
        normalized = self._default_state()

        positions_section = raw.get("positions")
        if not isinstance(positions_section, dict):
            positions_section = {}

        open_positions = deepcopy(raw.get("open_positions", positions_section.get("open", [])))
        closed_positions = deepcopy(raw.get("closed_positions", positions_section.get("closed", [])))

        bot_config = deepcopy(raw.get("bot_config", {}))
        if not isinstance(bot_config, dict):
            bot_config = {}

        for field in BOT_CONFIG_FIELDS:
            if field in raw and field not in bot_config:
                bot_config[field] = raw[field]

        explicit_profit_tracking = raw.get("profit_tracking")
        if not isinstance(explicit_profit_tracking, dict):
            explicit_profit_tracking = {}

        realized_from_records = sum(
            float(record.get("realized_profit_sol", 0.0) or 0.0)
            for record in closed_positions
            if isinstance(record, dict)
        )
        locked_from_records = sum(
            float(record.get("locked_profit_sol", 0.0) or 0.0)
            for record in closed_positions
            if isinstance(record, dict)
        )

        explicit_realized = float(explicit_profit_tracking.get("realized_profit_sol", 0.0) or 0.0)
        explicit_locked = float(explicit_profit_tracking.get("locked_profit_sol", 0.0) or 0.0)
        explicit_locked_balance = float(raw.get("locked_balance", 0.0) or 0.0)

        realized_total = max(explicit_realized, realized_from_records)
        locked_total = max(explicit_locked_balance, explicit_locked, locked_from_records)

        normalized["version"] = int(raw.get("version", normalized["version"]) or normalized["version"])
        normalized["updated_at"] = raw.get("updated_at") or _utcnow()
        normalized["bot_config"] = bot_config
        normalized["positions"] = {
            "open": open_positions,
            "closed": closed_positions,
        }
        normalized["open_positions"] = open_positions
        normalized["closed_positions"] = closed_positions
        normalized["trade_history"] = deepcopy(raw.get("trade_history", []))
        normalized["alerts"] = deepcopy(raw.get("alerts", []))
        normalized["scanner_feeds"] = deepcopy(raw.get("scanner_feeds", []))
        normalized["profit_tracking"] = {
            "realized_profit_sol": realized_total,
            "locked_profit_sol": locked_total,
        }
        normalized["locked_balance"] = locked_total

        for field, value in bot_config.items():
            normalized[field] = value

        return normalized

    def _read_json(self, path: Path) -> dict[str, Any]:
        return read_json_file(path)

    def _archive_corrupt_file(self, path: Path) -> Optional[Path]:
        return archive_corrupt_file(path, logger=self.logger)

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        write_json_state(self.path, payload, backup_path=self.backup_path, logger=self.logger)

    def save(self, snapshot: Optional[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            payload = self._normalize(snapshot)
            payload["updated_at"] = _utcnow()
            self._atomic_write(payload)
            self._data = payload
            self._dirty = False
            return deepcopy(payload)

    def load(self) -> dict[str, Any]:
        with self._lock:
            recovered_from_backup = False

            if not self.path.exists():
                if self.backup_path.exists():
                    try:
                        payload = self._normalize(
                            restore_json_from_backup(
                                self.path,
                                backup_path=self.backup_path,
                                default_factory=self._default_state,
                                logger=self.logger,
                            )
                        )
                        recovered_from_backup = True
                    except (json.JSONDecodeError, OSError, ValueError):
                        self._archive_corrupt_file(self.backup_path)
                        payload = self._default_state()
                    self._data = payload
                    if not recovered_from_backup:
                        self.save(payload)
                    return deepcopy(self._data)
                self._data = self._default_state()
                self.save(self._data)
                return deepcopy(self._data)

            try:
                payload = self._normalize(self._read_json(self.path))
            except (json.JSONDecodeError, OSError, ValueError):
                self._archive_corrupt_file(self.path)

                try:
                    payload = self._normalize(
                        restore_json_from_backup(
                            self.path,
                            backup_path=self.backup_path,
                            default_factory=self._default_state,
                            logger=self.logger,
                        )
                    )
                    recovered_from_backup = True
                except (json.JSONDecodeError, OSError, ValueError):
                    if self.backup_path.exists():
                        self._archive_corrupt_file(self.backup_path)
                    payload = self._default_state()
            else:
                self._data = payload

            if not self.path.exists() and not recovered_from_backup:
                self.save(payload)
            self._data = payload
            return deepcopy(self._data)

    def update(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            current = deepcopy(self._data if self._data else self.load())
            for key, value in kwargs.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key] = {**current[key], **deepcopy(value)}
                else:
                    current[key] = deepcopy(value)
            self._data = self._normalize(current)
            self._dirty = True
            return deepcopy(self._data)

    def _auto_save_loop(self) -> None:
        while not self._auto_save_stop.wait(self.auto_save_interval):
            try:
                callback = self._auto_save_callback
                if callback is not None:
                    callback()
                    continue

                with self._lock:
                    if self._dirty:
                        self.save(self._data)
            except Exception as exc:
                self.logger(f"Auto-save error: {exc}")

    def start_autosave(self, callback: Optional[Callable[[], None]] = None) -> None:
        with self._lock:
            if self._auto_save_thread and self._auto_save_thread.is_alive():
                return
            self._auto_save_callback = callback
            self._auto_save_stop.clear()
            self._auto_save_thread = threading.Thread(target=self._auto_save_loop, daemon=True)
            self._auto_save_thread.start()

    def stop_autosave(self) -> None:
        thread = self._auto_save_thread
        if thread is None:
            return

        self._auto_save_stop.set()
        thread.join(timeout=max(self.auto_save_interval, 0.1) + 0.2)
        self._auto_save_thread = None
        self._auto_save_callback = None

    def start_auto_save(self) -> None:
        self.start_autosave()

    def stop_auto_save(self) -> None:
        self.stop_autosave()

    def get_locked_balance(self) -> float:
        with self._lock:
            if not self.path.exists():
                return 0.0
            payload = self.load()
            return float(payload.get("locked_balance", 0.0) or 0.0)

    def lock_profit(self, amount: float, lock_pct: Optional[float] = None) -> float:
        amount = float(amount)
        if amount <= 0:
            return 0.0

        if lock_pct is None:
            lock_pct = float(os.environ.get(LOCK_PCT_ENV, DEFAULT_LOCK_PCT) or DEFAULT_LOCK_PCT)

        locked_amount = amount * float(lock_pct)

        with self._lock:
            payload = self.load() if self.path.exists() else self._default_state()
            current_locked = float(payload.get("locked_balance", 0.0) or 0.0)
            payload["locked_balance"] = current_locked + locked_amount
            profit_tracking = payload.setdefault("profit_tracking", {})
            profit_tracking["locked_profit_sol"] = float(payload["locked_balance"])
            self.save(payload)

        return locked_amount

    def _serialize_position(self, position: Any) -> dict[str, Any]:
        if isinstance(position, dict):
            return deepcopy(position)
        if is_dataclass(position):
            return asdict(position)
        raise TypeError(f"Unsupported position payload: {type(position)!r}")

    def _serialize_closed_record(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = {}
        for key, value in record.items():
            if key == "position":
                payload[key] = self._serialize_position(value)
            else:
                payload[key] = deepcopy(value)
        return payload

    def _serialize_feed(self, feed: Any) -> dict[str, Any]:
        history = []
        for point in list(getattr(feed, "history", [])):
            history.append(
                {
                    "timestamp": float(getattr(point, "timestamp", 0.0) or 0.0),
                    "price": float(getattr(point, "price", 0.0) or 0.0),
                    "volume_estimate": float(getattr(point, "volume_estimate", 0.0) or 0.0),
                }
            )

        return {
            "pair": str(getattr(feed, "pair_name", "")),
            "input_mint": str(getattr(feed, "input_mint", "")),
            "output_mint": str(getattr(feed, "output_mint", "")),
            "history": history,
        }

    def save_trader_state(self, trader: Any) -> dict[str, Any]:
        open_positions = []
        for position in getattr(getattr(trader, "risk_manager", None), "positions", []):
            if getattr(position, "status", "") != "open":
                continue
            open_positions.append(
                {
                    "position": self._serialize_position(position),
                    "meta": deepcopy(getattr(trader, "position_meta", {}).get(position.pair, {})),
                }
            )

        closed_positions = [
            self._serialize_closed_record(record)
            for record in getattr(getattr(trader, "risk_manager", None), "closed_positions", [])
        ]

        snapshot = {
            "version": 1,
            "dry_run": getattr(trader, "dry_run", True),
            "cycle": getattr(trader, "cycle", 0),
            "entry_amount_sol": getattr(trader, "entry_amount_sol", None),
            "enter_on": getattr(trader, "enter_on", None),
            "max_open_positions": getattr(trader, "max_open_positions", None),
            "scan_interval_secs": getattr(trader, "scan_interval_secs", None),
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "trade_history": deepcopy(getattr(getattr(trader, "executor", None), "trade_history", [])),
            "alerts": deepcopy(getattr(getattr(trader, "scanner", None), "alerts", [])),
            "scanner_feeds": [
                self._serialize_feed(feed)
                for feed in getattr(getattr(trader, "scanner", None), "feeds", [])
            ],
        }

        return self.save(snapshot)

    def _deserialize_position(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload

        from .risk import Position

        return Position(
            pair=str(payload.get("pair", "")),
            input_mint=str(payload.get("input_mint", "")),
            output_mint=str(payload.get("output_mint", "")),
            entry_price=float(payload.get("entry_price", 0.0) or 0.0),
            amount_sol=float(payload.get("amount_sol", 0.0) or 0.0),
            entry_time=float(payload.get("entry_time", 0.0) or 0.0),
            stop_loss_pct=float(payload.get("stop_loss_pct", 0.05) or 0.05),
            take_profit_pct=float(payload.get("take_profit_pct", 0.15) or 0.15),
            trailing_stop_pct=float(payload.get("trailing_stop_pct", 0.03) or 0.03),
            highest_price=float(payload.get("highest_price", 0.0) or 0.0),
            status=str(payload.get("status", "open")),
            notional=float(payload.get("notional", 0.0) or 0.0),
            tx_buy=payload.get("tx_buy"),
        )

    def _restore_feed(self, trader: Any, payload: dict[str, Any]) -> Any:
        from .oracle import PriceFeed, PricePoint

        pair = str(payload.get("pair", ""))
        input_mint = str(payload.get("input_mint", ""))
        output_mint = str(payload.get("output_mint", ""))

        existing = None
        for feed in getattr(getattr(trader, "scanner", None), "feeds", []):
            if getattr(feed, "pair_name", None) == pair:
                existing = feed
                break

        if existing is None:
            existing = PriceFeed(pair_name=pair, input_mint=input_mint, output_mint=output_mint)
            trader.scanner.feeds.append(existing)

        history = getattr(existing, "history", None)
        if history is None:
            history = []
            setattr(existing, "history", history)

        clear = getattr(history, "clear", None)
        if callable(clear):
            clear()
        else:
            history[:] = []

        for point in payload.get("history", []):
            history.append(
                PricePoint(
                    timestamp=float(point.get("timestamp", 0.0) or 0.0),
                    price=float(point.get("price", 0.0) or 0.0),
                    volume_estimate=float(point.get("volume_estimate", 0.0) or 0.0),
                )
            )

        return existing

    def load_into_trader(self, trader: Any) -> dict[str, Any]:
        state = self.load()

        bot_config = state.get("bot_config", {})
        if isinstance(bot_config, dict):
            for field in BOT_CONFIG_FIELDS:
                if field in bot_config:
                    setattr(trader, field, bot_config[field])

        trader.cycle = int(state.get("cycle", getattr(trader, "cycle", 0)) or 0)

        executor = getattr(trader, "executor", None)
        if executor is not None:
            executor.trade_history = deepcopy(state.get("trade_history", []))

        scanner = getattr(trader, "scanner", None)
        if scanner is not None:
            scanner.alerts = deepcopy(state.get("alerts", []))
            for feed_payload in state.get("scanner_feeds", []):
                if isinstance(feed_payload, dict):
                    self._restore_feed(trader, feed_payload)
            if hasattr(trader, "_index_scanner_feeds"):
                trader._index_scanner_feeds()

        risk_manager = getattr(trader, "risk_manager", None)
        if risk_manager is not None:
            risk_manager.positions = []
            risk_manager.closed_positions = []
            risk_manager.price_feeds = {}

        trader.position_meta = {}

        for record in state.get("open_positions", []):
            if not isinstance(record, dict):
                continue
            position = self._deserialize_position(record.get("position", {}))
            if risk_manager is not None:
                risk_manager.positions.append(position)
            meta = deepcopy(record.get("meta", {}))
            trader.position_meta[position.pair] = meta
            if risk_manager is not None and hasattr(trader, "_resolve_pair") and hasattr(trader, "_ensure_scanner_feed"):
                pair_config = trader._resolve_pair(position.pair)
                input_mint = meta.get("scan_input_mint") or position.input_mint
                output_mint = meta.get("scan_output_mint") or position.output_mint
                if pair_config is not None:
                    input_mint, output_mint = pair_config
                risk_manager.price_feeds[position.pair] = trader._ensure_scanner_feed(
                    position.pair,
                    input_mint,
                    output_mint,
                )

        for record in state.get("closed_positions", []):
            if not isinstance(record, dict):
                continue
            restored = deepcopy(record)
            restored["position"] = self._deserialize_position(record.get("position", {}))
            if risk_manager is not None:
                risk_manager.closed_positions.append(restored)

        return state


__all__ = [
    "DEFAULT_LOCK_PCT",
    "LOCK_PCT_ENV",
    "StateManager",
]
