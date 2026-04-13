import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

from src.ml.model_monitor import ModelMonitor

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
    component_breakdown: Dict[str, float] = field(default_factory=dict)

class SignalEnsemble:
    """
    Combines signals from multiple strategies and models (mean reversion, momentum, 
    sentiment, ML predictor, regime) into a single weighted score.
    Higher confidence = larger position size multiplier.
    """
    def __init__(self, weights: Optional[Dict[str, float]] = None):
        # Default weights if not provided
        self.weights = weights or {
            "mean_reversion": 1.0,
            "momentum": 1.2,
            "sentiment": 0.8,
            "ml_predictor": 1.5,
            "regime": 1.0,
        }
        self.signals: Dict[str, StrategySignal] = {}

    def update_signal(self, strategy_name: str, direction: SignalDirection, confidence: float) -> None:
        """Update the current signal for a specific strategy."""
        weight = self.weights.get(strategy_name, 1.0)
        
        # Clamp confidence between 0.0 and 1.0
        confidence = max(0.0, min(1.0, confidence))
        
        self.signals[strategy_name] = StrategySignal(
            direction=direction, 
            confidence=confidence, 
            weight=weight
        )
        logger.debug(f"Ensemble updated {strategy_name} signal: {direction.name} (Conf: {confidence:.2f})")

    def evaluate(self) -> EnsembleResult:
        """
        Evaluate all current signals and return a combined trading decision.
        """
        if not self.signals:
            return EnsembleResult(SignalDirection.NEUTRAL, 0.0, 0.0, 0.0, {})

        total_weight = sum(sig.weight for sig in self.signals.values())
        if total_weight == 0:
            return EnsembleResult(SignalDirection.NEUTRAL, 0.0, 0.0, 0.0, {})

        weighted_score_sum = sum(sig.score * sig.weight for sig in self.signals.values())
        combined_score = weighted_score_sum / total_weight

        # Breakdown for the dashboard
        breakdown = {
            name: sig.score * sig.weight / total_weight if total_weight > 0 else 0.0
            for name, sig in self.signals.items()
        }

        # Determine direction
        if combined_score > 0.2:
            direction = SignalDirection.BULLISH
        elif combined_score < -0.2:
            direction = SignalDirection.BEARISH
        else:
            direction = SignalDirection.NEUTRAL

        # Confidence is the absolute value of the combined score (how strongly it leans one way)
        combined_confidence = abs(combined_score)
        
        # Position size scales with confidence (e.g. nonlinear mapping could be used, keeping it simple here)
        position_size_multiplier = combined_confidence if direction != SignalDirection.NEUTRAL else 0.0

        # Apply ML model performance multiplier
        ml_multiplier = self.monitor.get_position_size_multiplier()
        position_size_multiplier *= ml_multiplier

        return EnsembleResult(
            direction=direction,
            combined_confidence=combined_confidence,
            combined_score=combined_score,
            position_size_multiplier=position_size_multiplier,
            component_breakdown=breakdown
        )
