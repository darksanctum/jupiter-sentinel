import json
import logging
from pathlib import Path
from typing import Dict, List
from datetime import datetime

logger = logging.getLogger(__name__)

class ModelMonitor:
    """Tracks ML model performance and adjusts position sizing based on accuracy."""
    
    def __init__(self, file_path: str | Path = "data/ml_performance.json", window_size: int = 100):
        self.file_path = Path(file_path)
        self.window_size = window_size
        self.history: List[bool] = []
        self._load()

    def _load(self) -> None:
        if self.file_path.exists():
            try:
                with open(self.file_path, "r") as f:
                    data = json.load(f)
                    self.history = data.get("history", [])
            except Exception as e:
                logger.error(f"Error loading model monitor data: {e}")
                self.history = []

    def _save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.file_path, "w") as f:
                json.dump({
                    "history": self.history[-self.window_size:],
                    "accuracy": self.get_accuracy(),
                    "updated_at": datetime.utcnow().isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving model monitor data: {e}")

    def record_result(self, is_correct: bool) -> None:
        """Record whether a prediction was correct."""
        self.history.append(is_correct)
        if len(self.history) > self.window_size:
            self.history.pop(0)
        self._save()

    def get_accuracy(self) -> float:
        if not self.history:
            # Assume 50% accuracy if no history
            return 0.50
        return sum(self.history) / len(self.history)

    def get_position_size_multiplier(self) -> float:
        """
        If prediction accuracy drops below 55%, reduce position sizes.
        If accuracy is above 65%, increase sizes.
        """
        # If we don't have enough history, just return 1.0
        if len(self.history) < 10:
            return 1.0
            
        accuracy = self.get_accuracy()
        if accuracy < 0.55:
            return 0.5
        elif accuracy > 0.65:
            return 1.5
        return 1.0
        
    def get_status(self) -> Dict[str, str]:
        acc = self.get_accuracy()
        mult = self.get_position_size_multiplier()
        return {
            "accuracy": f"{acc * 100:.1f}%",
            "multiplier": f"{mult}x",
            "samples": str(len(self.history))
        }
