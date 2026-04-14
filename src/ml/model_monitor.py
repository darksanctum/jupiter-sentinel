"""Track model quality and derive runtime sizing multipliers."""

import logging
import json
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


class ModelMonitor:
    """Tracks ML model performance and adjusts position sizing based on accuracy."""

    def __init__(
        self,
        file_path: str | Path = "data/ml_performance.json",
        window_size: int = 100,
    ) -> None:
        self.file_path = Path(file_path)
        self.window_size = window_size
        self.history: list[bool] = []
        self._load()

    def _load(self) -> None:
        """Load persisted monitoring state from disk if it exists."""
        if self.file_path.exists():
            try:
                with self.file_path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
                    self.history = data.get("history", [])
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.error("Error loading model monitor data: %s", exc)
                self.history = []

    def _save(self) -> None:
        """Persist the rolling monitoring window to disk."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.file_path.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "history": self.history[-self.window_size :],
                        "accuracy": self.get_accuracy(),
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                    handle,
                    indent=2,
                )
        except OSError as exc:
            logger.error("Error saving model monitor data: %s", exc)

    def record_result(self, is_correct: bool) -> None:
        """Record whether a prediction was correct."""
        self.history.append(is_correct)
        if len(self.history) > self.window_size:
            self.history.pop(0)
        self._save()

    def get_accuracy(self) -> float:
        """Return rolling directional accuracy for tracked model outputs."""
        if not self.history:
            return 0.50
        return sum(self.history) / len(self.history)

    def get_position_size_multiplier(self) -> float:
        """
        If prediction accuracy drops below 55%, reduce position sizes.
        If accuracy is above 65%, increase sizes.
        """
        if len(self.history) < 10:
            return 1.0

        accuracy = self.get_accuracy()
        if accuracy < 0.55:
            return 0.5
        if accuracy > 0.65:
            return 1.5
        return 1.0

    def get_status(self) -> dict[str, str]:
        """Return human-readable model-monitor status for dashboards."""
        acc = self.get_accuracy()
        mult = self.get_position_size_multiplier()
        return {
            "accuracy": f"{acc * 100:.1f}%",
            "multiplier": f"{mult}x",
            "samples": str(len(self.history)),
        }
