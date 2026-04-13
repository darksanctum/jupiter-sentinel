import json
import logging
import random
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

from src.config import DATA_DIR, STOP_LOSS_BPS, TAKE_PROFIT_BPS

OPTIMIZATIONS_FILE = DATA_DIR / "optimizations.json"
logger = logging.getLogger(__name__)

class SelfOptimizer:
    """
    Automatically tunes strategy parameters based on recent performance.
    Uses a simple gradient-free optimization (random search) guided by heuristics.
    """
    def __init__(self, file_path: Path = OPTIMIZATIONS_FILE):
        self.file_path = file_path
        self.history = self._load_history()
        
        # Default starting params based on config
        self.current_params = {
            "stop_loss_bps": STOP_LOSS_BPS,
            "take_profit_bps": TAKE_PROFIT_BPS
        }
        
        if self.history and len(self.history) > 0:
            last_entry = self.history[-1]
            if "new_params" in last_entry:
                self.current_params = last_entry["new_params"]

    def _load_history(self) -> list:
        if self.file_path.exists():
            try:
                with self.file_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Failed to decode {self.file_path}. Starting fresh.")
                return []
        return []

    def _save_history(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=2)

    def get_current_params(self) -> Dict[str, Any]:
        """Returns the currently active, optimized parameters."""
        return self.current_params

    def evaluate_performance(self, win_rate: float, sl_hit_rate: float, tp_hit_rate: float, profit_factor: float) -> Dict[str, Any]:
        """
        Evaluates recent performance and proposes new parameters using gradient-free optimization
        with heuristic guidance.
        """
        old_params = self.current_params.copy()
        new_params = old_params.copy()

        # Heuristic rules
        if sl_hit_rate > 0.5:  # Stop-loss getting hit too often (>50%)
            logger.info("Stop-loss hit too often. Widening stop-loss slightly.")
            new_params["stop_loss_bps"] = int(new_params["stop_loss_bps"] * 1.1)
        elif sl_hit_rate < 0.1:
            logger.info("Stop-loss rarely hit. Tightening stop-loss for better risk management.")
            new_params["stop_loss_bps"] = int(new_params["stop_loss_bps"] * 0.95)
        
        if tp_hit_rate > 0.5:  # Take-profit too tight (hit very easily)
            logger.info("Take-profit hit very often (too tight). Loosening take-profit.")
            new_params["take_profit_bps"] = int(new_params["take_profit_bps"] * 1.1)
        elif tp_hit_rate < 0.1: # Take profit rarely hit (too wide)
            logger.info("Take-profit rarely hit. Tightening take-profit.")
            new_params["take_profit_bps"] = int(new_params["take_profit_bps"] * 0.9)

        # Gradient-free optimization (random search)
        # Apply a small random mutation to explore parameter space
        mutation_rate = 0.05  # 5% random variation
        new_params["stop_loss_bps"] = int(new_params["stop_loss_bps"] * (1 + random.uniform(-mutation_rate, mutation_rate)))
        new_params["take_profit_bps"] = int(new_params["take_profit_bps"] * (1 + random.uniform(-mutation_rate, mutation_rate)))

        # Ensure parameters stay within reasonable bounds
        new_params["stop_loss_bps"] = max(100, min(new_params["stop_loss_bps"], 3000))
        new_params["take_profit_bps"] = max(200, min(new_params["take_profit_bps"], 10000))

        logger.info(f"Self-optimizer tuning: {old_params} -> {new_params}")

        # Track changes in history
        history_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "performance_metrics": {
                "win_rate": win_rate,
                "sl_hit_rate": sl_hit_rate,
                "tp_hit_rate": tp_hit_rate,
                "profit_factor": profit_factor
            },
            "old_params": old_params,
            "new_params": new_params
        }

        self.history.append(history_entry)
        self.current_params = new_params
        self._save_history()

        return new_params
