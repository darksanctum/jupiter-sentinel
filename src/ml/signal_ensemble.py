"""Signal ensemble utilities for combining model and strategy outputs."""

import logging
from dataclasses import dataclass, field
from enum import Enum

from .model_monitor import ModelMonitor

logger = logging.getLogger(__name__)


class SignalDirection(Enum):
    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1


@dataclass
class StrategySignal:
    """Represents a signal from a single strategy/predictor."""

    direction: SignalDirection
    confidence: float  # 0.0 to 1.0
    weight: float = 1.0

    @property
    def score(self) -> float:
        """Raw score from -1.0 to 1.0 adjusted by confidence."""
        return self.direction.value * self.confidence


@dataclass
class EnsembleResult:
    """The combined output of the ensemble."""

    direction: SignalDirection
    combined_confidence: float  # 0.0 to 1.0
    combined_score: float  # -1.0 to 1.0
    position_size_multiplier: float  # 0.0 to 1.0 (scales with confidence)
    component_breakdown: dict[str, float] = field(default_factory=dict)


DEFAULT_SIGNAL_WEIGHTS = {
    "mean_reversion": 1.0,
    "momentum": 1.2,
    "sentiment": 0.8,
    "ml_predictor": 1.5,
    "regime": 1.0,
}
SIGNAL_DIRECTION_THRESHOLD = 0.2


class SignalEnsemble:
    """
    Combines signals from multiple strategies and models (mean reversion, momentum,
    sentiment, ML predictor, regime) into a single weighted score.
    Higher confidence = larger position size multiplier.
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        monitor: ModelMonitor | None = None,
    ) -> None:
        self.weights = dict(weights or DEFAULT_SIGNAL_WEIGHTS)
        self.monitor = monitor or ModelMonitor()
        self.signals: dict[str, StrategySignal] = {}

    def update_signal(
        self,
        strategy_name: str,
        direction: SignalDirection,
        confidence: float,
    ) -> None:
        """Update the current signal for a specific strategy."""
        weight = self.weights.get(strategy_name, 1.0)
        confidence = max(0.0, min(1.0, confidence))

        self.signals[strategy_name] = StrategySignal(
            direction=direction,
            confidence=confidence,
            weight=weight,
        )
        logger.debug(
            "Ensemble updated %s signal: %s (conf=%.2f)",
            strategy_name,
            direction.name,
            confidence,
        )

    @staticmethod
    def _resolve_direction(combined_score: float) -> SignalDirection:
        """Map a combined score into a categorical trade direction."""
        if combined_score > SIGNAL_DIRECTION_THRESHOLD:
            return SignalDirection.BULLISH
        if combined_score < -SIGNAL_DIRECTION_THRESHOLD:
            return SignalDirection.BEARISH
        return SignalDirection.NEUTRAL

    def evaluate(self) -> EnsembleResult:
        """Evaluate all current signals and return a combined trading decision."""
        if not self.signals:
            return EnsembleResult(SignalDirection.NEUTRAL, 0.0, 0.0, 0.0, {})

        total_weight = sum(sig.weight for sig in self.signals.values())
        if total_weight == 0:
            return EnsembleResult(SignalDirection.NEUTRAL, 0.0, 0.0, 0.0, {})

        weighted_score_sum = sum(sig.score * sig.weight for sig in self.signals.values())
        combined_score = weighted_score_sum / total_weight

        breakdown = {
            name: sig.score * sig.weight / total_weight if total_weight > 0 else 0.0
            for name, sig in self.signals.items()
        }
        direction = self._resolve_direction(combined_score)
        combined_confidence = abs(combined_score)

        position_size_multiplier = 0.0
        if direction != SignalDirection.NEUTRAL:
            position_size_multiplier = (
                combined_confidence * self.monitor.get_position_size_multiplier()
            )

        return EnsembleResult(
            direction=direction,
            combined_confidence=combined_confidence,
            combined_score=combined_score,
            position_size_multiplier=position_size_multiplier,
            component_breakdown=breakdown,
        )
