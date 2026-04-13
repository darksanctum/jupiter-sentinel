"""Decision-tree predictor for the next detected market regime."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from ..config import DATA_DIR
from ..oracle import PriceFeed, PricePoint
from ..regime_detector import MarketRegime, RegimeDetector
from ..resilience import atomic_write_text
from .feature_engineer import (
    DEFAULT_CONFIG,
    FeatureConfig,
    extract_features_from_history,
    feature_names as market_feature_names,
)

DEFAULT_MODEL_DIR = DATA_DIR / "models"
REGIME_CLASS_NAMES = tuple(regime.value for regime in MarketRegime)
REGIME_TO_INDEX = {name: index for index, name in enumerate(REGIME_CLASS_NAMES)}


@dataclass(frozen=True)
class RegimeTreeConfig:
    """Training configuration for the scratch decision tree."""

    max_depth: int = 5
    min_samples_split: int = 12
    min_samples_leaf: int = 6
    min_impurity_decrease: float = 1e-5
    test_fraction: float = 0.25
    min_samples: int = 40
    min_history: int | None = None
    regime_lookback: int = 12

    def __post_init__(self) -> None:
        if not isinstance(self.max_depth, int) or self.max_depth < 1:
            raise ValueError("max_depth must be an integer >= 1")
        if not isinstance(self.min_samples_split, int) or self.min_samples_split < 2:
            raise ValueError("min_samples_split must be an integer >= 2")
        if not isinstance(self.min_samples_leaf, int) or self.min_samples_leaf < 1:
            raise ValueError("min_samples_leaf must be an integer >= 1")
        if self.min_samples_split < self.min_samples_leaf * 2:
            raise ValueError(
                "min_samples_split must be at least twice min_samples_leaf"
            )
        if (
            not math.isfinite(self.min_impurity_decrease)
            or self.min_impurity_decrease < 0
        ):
            raise ValueError("min_impurity_decrease must be a finite float >= 0")
        if not math.isfinite(self.test_fraction) or not 0 < self.test_fraction < 1:
            raise ValueError("test_fraction must be a finite float between 0 and 1")
        if not isinstance(self.min_samples, int) or self.min_samples < 2:
            raise ValueError("min_samples must be an integer >= 2")
        if self.min_history is not None and (
            not isinstance(self.min_history, int) or self.min_history < 2
        ):
            raise ValueError("min_history must be None or an integer >= 2")
        if not isinstance(self.regime_lookback, int) or self.regime_lookback < 1:
            raise ValueError("regime_lookback must be an integer >= 1")


@dataclass(frozen=True)
class RegimeEvaluationReport:
    """Basic evaluation metrics for the multiclass regime classifier."""

    accuracy: float
    log_loss: float
    sample_count: int
    average_confidence: float


@dataclass(frozen=True)
class RegimePrediction:
    """One predicted regime plus class probabilities."""

    regime: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class RegimeDataset:
    """Feature matrix and labels for next-regime prediction."""

    pair_name: str
    feature_names: tuple[str, ...]
    class_names: tuple[str, ...]
    X: np.ndarray
    y: np.ndarray
    timestamps: tuple[str, ...]
    current_regimes: tuple[str, ...]
    next_regimes: tuple[str, ...]

    @property
    def sample_count(self) -> int:
        """Return the number of supervised samples."""
        return int(self.y.shape[0])


@dataclass(frozen=True)
class RegimeTrainingResult:
    """Structured output from a train/evaluate/save run."""

    pair_name: str
    source: str
    sample_count: int
    train_metrics: RegimeEvaluationReport
    test_metrics: RegimeEvaluationReport
    model_path: Path
    model: "RegimePredictor"


@dataclass
class _BestSplit:
    feature_index: int
    threshold: float
    impurity_gain: float


@dataclass
class _TreeNode:
    prediction_index: int
    class_counts: list[int]
    sample_count: int
    impurity: float
    feature_index: int | None = None
    threshold: float | None = None
    left: "_TreeNode | None" = None
    right: "_TreeNode | None" = None

    @property
    def is_leaf(self) -> bool:
        """Return whether the node has children."""
        return self.feature_index is None or self.left is None or self.right is None

    def probabilities(self) -> list[float]:
        """Return the class distribution represented by this node."""
        total = sum(self.class_counts)
        if total <= 0:
            return [0.0 for _ in self.class_counts]
        return [count / total for count in self.class_counts]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the node recursively."""
        payload: dict[str, Any] = {
            "prediction_index": self.prediction_index,
            "class_counts": list(self.class_counts),
            "sample_count": self.sample_count,
            "impurity": self.impurity,
            "feature_index": self.feature_index,
            "threshold": self.threshold,
        }
        if self.left is not None:
            payload["left"] = self.left.to_dict()
        if self.right is not None:
            payload["right"] = self.right.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "_TreeNode":
        """Restore a serialized node recursively."""
        return cls(
            prediction_index=int(payload["prediction_index"]),
            class_counts=[int(value) for value in payload["class_counts"]],
            sample_count=int(payload["sample_count"]),
            impurity=float(payload["impurity"]),
            feature_index=(
                None
                if payload.get("feature_index") is None
                else int(payload["feature_index"])
            ),
            threshold=(
                None if payload.get("threshold") is None else float(payload["threshold"])
            ),
            left=(
                cls.from_dict(payload["left"])
                if isinstance(payload.get("left"), dict)
                else None
            ),
            right=(
                cls.from_dict(payload["right"])
                if isinstance(payload.get("right"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class _HistoryFeed:
    history: Sequence[PricePoint]


@dataclass
class RegimePredictor:
    """Scratch decision tree trained to predict the next market regime."""

    pair_name: str
    feature_names: tuple[str, ...]
    class_names: tuple[str, ...]
    root: _TreeNode
    training_config: RegimeTreeConfig = field(default_factory=RegimeTreeConfig)
    feature_config: FeatureConfig = field(default_factory=lambda: DEFAULT_CONFIG)
    detector_config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.feature_names:
            raise ValueError("feature_names must not be empty")
        if not self.class_names:
            raise ValueError("class_names must not be empty")
        if len(self.root.class_counts) != len(self.class_names):
            raise ValueError("tree root class counts must align with class_names")

    @classmethod
    def fit(
        cls,
        X: np.ndarray,
        y: np.ndarray,
        *,
        pair_name: str,
        feature_names_: Sequence[str],
        class_names_: Sequence[str] = REGIME_CLASS_NAMES,
        training_config: RegimeTreeConfig | None = None,
        feature_config: FeatureConfig | None = None,
        detector: RegimeDetector | None = None,
    ) -> "RegimePredictor":
        """Fit a simple CART-style decision tree without external ML libraries."""
        cfg = training_config or RegimeTreeConfig()
        feat_cfg = feature_config or DEFAULT_CONFIG
        detector_ = detector or RegimeDetector()
        class_names = tuple(class_names_)

        matrix = _ensure_matrix(X)
        labels = _ensure_label_vector(y, class_count=len(class_names))
        if matrix.shape[0] != labels.shape[0]:
            raise ValueError("X and y must contain the same number of rows")
        if matrix.shape[0] < 2:
            raise ValueError("At least two training rows are required")

        root = _grow_tree(
            matrix,
            labels,
            depth=0,
            config=cfg,
            class_count=len(class_names),
        )
        return cls(
            pair_name=pair_name,
            feature_names=tuple(feature_names_),
            class_names=class_names,
            root=root,
            training_config=cfg,
            feature_config=feat_cfg,
            detector_config=_detector_to_dict(detector_),
            metadata={
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "tree_depth": _tree_depth(root),
                "leaf_count": _leaf_count(root),
            },
        )

    def predict_proba(self, X: np.ndarray | Sequence[float]) -> np.ndarray:
        """Predict the class probabilities for one or more feature rows."""
        matrix = _ensure_matrix(X, expected_width=len(self.feature_names))
        probabilities = [_traverse_tree(self.root, row).probabilities() for row in matrix]
        return np.asarray(probabilities, dtype=float)

    def predict_indices(self, X: np.ndarray | Sequence[float]) -> np.ndarray:
        """Predict encoded regime indices."""
        probabilities = self.predict_proba(X)
        return np.argmax(probabilities, axis=1).astype(int)

    def predict(self, X: np.ndarray | Sequence[float]) -> np.ndarray:
        """Predict regime labels."""
        indices = self.predict_indices(X)
        return np.asarray([self.class_names[index] for index in indices], dtype=object)

    def predict_with_confidence(
        self, X: np.ndarray | Sequence[float]
    ) -> list[RegimePrediction]:
        """Predict labels and confidence derived from leaf class frequencies."""
        probabilities = self.predict_proba(X)
        indices = np.argmax(probabilities, axis=1)
        predictions: list[RegimePrediction] = []
        for row_probabilities, index in zip(probabilities, indices):
            probability_map = {
                name: float(probability)
                for name, probability in zip(self.class_names, row_probabilities)
            }
            predictions.append(
                RegimePrediction(
                    regime=self.class_names[int(index)],
                    confidence=float(row_probabilities[int(index)]),
                    probabilities=probability_map,
                )
            )
        return predictions

    def predict_next_regime(
        self,
        feed_or_history: PriceFeed | Iterable[Any],
        *,
        detector: RegimeDetector | None = None,
    ) -> RegimePrediction:
        """Build a feature row from live history and predict the next regime."""
        feature_row = build_regime_feature_row_from_history(
            getattr(feed_or_history, "history", feed_or_history),
            feature_config=self.feature_config,
            detector=detector or _detector_from_dict(self.detector_config),
            regime_lookback=self.training_config.regime_lookback,
            ordered_feature_names=self.feature_names,
        )
        return self.predict_with_confidence(feature_row)[0]

    def evaluate(self, X: np.ndarray, y: np.ndarray) -> RegimeEvaluationReport:
        """Evaluate the model on a feature matrix and encoded label vector."""
        labels = _ensure_label_vector(y, class_count=len(self.class_names))
        probabilities = self.predict_proba(X)
        predictions = np.argmax(probabilities, axis=1).astype(int)
        confidences = np.max(probabilities, axis=1) if probabilities.size else np.array([])
        accuracy = float(np.mean(predictions == labels)) if labels.size else 0.0
        return RegimeEvaluationReport(
            accuracy=accuracy,
            log_loss=_multiclass_log_loss(labels, probabilities),
            sample_count=int(labels.shape[0]),
            average_confidence=float(np.mean(confidences)) if confidences.size else 0.0,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the model to a JSON-compatible payload."""
        return {
            "model_type": "decision_tree_regime",
            "version": 1,
            "pair_name": self.pair_name,
            "feature_names": list(self.feature_names),
            "class_names": list(self.class_names),
            "tree": self.root.to_dict(),
            "training_config": asdict(self.training_config),
            "feature_config": asdict(self.feature_config),
            "detector_config": dict(self.detector_config),
            "metadata": dict(self.metadata),
        }

    def save(self, path: Path | str) -> Path:
        """Persist the model as JSON."""
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        return atomic_write_text(path, payload, encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "RegimePredictor":
        """Restore a model from JSON."""
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)

        return cls(
            pair_name=payload["pair_name"],
            feature_names=tuple(payload["feature_names"]),
            class_names=tuple(payload["class_names"]),
            root=_TreeNode.from_dict(payload["tree"]),
            training_config=RegimeTreeConfig(**payload["training_config"]),
            feature_config=FeatureConfig(**payload["feature_config"]),
            detector_config=dict(payload.get("detector_config", {})),
            metadata=dict(payload.get("metadata", {})),
        )


def regime_feature_names() -> list[str]:
    """Return the deterministic feature order for regime-history features."""
    names: list[str] = []
    for class_name in REGIME_CLASS_NAMES:
        label = class_name.lower()
        names.append(f"current_regime_{label}")
    for class_name in REGIME_CLASS_NAMES:
        label = class_name.lower()
        names.append(f"previous_regime_{label}")
    names.extend(
        [
            "regime_streak",
            "regime_change_rate",
        ]
    )
    for class_name in REGIME_CLASS_NAMES:
        label = class_name.lower()
        names.append(f"regime_ratio_{label}")
    return names


def recommended_regime_min_history(
    feature_config: FeatureConfig | None = None,
    *,
    detector: RegimeDetector | None = None,
    regime_lookback: int = 12,
) -> int:
    """Return a conservative minimum history length for stable features."""
    cfg = feature_config or DEFAULT_CONFIG
    detector_ = detector or RegimeDetector()
    return max(
        max(cfg.sma_periods),
        cfg.rsi_period + 1,
        cfg.macd_slow_period + cfg.macd_signal_period,
        cfg.bollinger_period,
        cfg.volume_ratio_period + 1,
        cfg.momentum_window + 1,
        cfg.volatility_window + cfg.volatility_lookback,
        detector_.slow_window,
        regime_lookback,
    )


def build_regime_feature_row_from_history(
    history: Iterable[Any],
    *,
    feature_config: FeatureConfig | None = None,
    detector: RegimeDetector | None = None,
    regime_lookback: int = 12,
    ordered_feature_names: Sequence[str] | None = None,
) -> np.ndarray:
    """Build one combined feature row from price history and detected regimes."""
    feat_cfg = feature_config or DEFAULT_CONFIG
    detector_ = detector or RegimeDetector()
    points = _normalize_history_points(history)
    required_history = recommended_regime_min_history(
        feat_cfg,
        detector=detector_,
        regime_lookback=regime_lookback,
    )
    if len(points) < required_history:
        raise ValueError(
            f"Need at least {required_history} valid price points; received {len(points)}."
        )

    combined_names = tuple(
        ordered_feature_names
        if ordered_feature_names is not None
        else market_feature_names(feat_cfg) + regime_feature_names()
    )
    regime_history = _detect_regime_history(points, detector_)
    feature_map = _build_combined_feature_map(
        points,
        regime_history,
        feature_config=feat_cfg,
        regime_lookback=regime_lookback,
    )
    return np.asarray(
        [[float(feature_map[name]) for name in combined_names]],
        dtype=float,
    )


def build_regime_dataset(
    rows: Sequence[Any],
    *,
    pair_name: str | None = None,
    feature_config: FeatureConfig | None = None,
    training_config: RegimeTreeConfig | None = None,
    detector: RegimeDetector | None = None,
    min_history: int | None = None,
) -> RegimeDataset:
    """Build a supervised dataset for next-regime prediction."""
    ordered_rows = list(rows)
    if len(ordered_rows) < 2:
        raise ValueError("At least two rows are required to build a regime dataset")

    feat_cfg = feature_config or DEFAULT_CONFIG
    tree_cfg = training_config or RegimeTreeConfig()
    detector_ = detector or RegimeDetector()
    selected_pair = pair_name or _infer_default_pair(ordered_rows)
    points, timestamps_by_point = _extract_pair_history(ordered_rows, selected_pair)
    required_history = (
        min_history
        if min_history is not None
        else (
            tree_cfg.min_history
            if tree_cfg.min_history is not None
            else recommended_regime_min_history(
                feat_cfg,
                detector=detector_,
                regime_lookback=tree_cfg.regime_lookback,
            )
        )
    )
    feature_name_order = tuple(market_feature_names(feat_cfg) + regime_feature_names())
    regimes = _detect_regime_history(points, detector_)

    feature_rows: list[list[float]] = []
    labels: list[int] = []
    timestamps: list[str] = []
    current_regimes: list[str] = []
    next_regimes: list[str] = []

    for index in range(required_history - 1, len(points) - 1):
        history = points[: index + 1]
        regime_history = regimes[: index + 1]
        feature_map = _build_combined_feature_map(
            history,
            regime_history,
            feature_config=feat_cfg,
            regime_lookback=tree_cfg.regime_lookback,
        )
        feature_rows.append(
            [float(feature_map[name]) for name in feature_name_order]
        )
        next_regime = regimes[index + 1]
        labels.append(REGIME_TO_INDEX[next_regime])
        timestamps.append(timestamps_by_point[index])
        current_regimes.append(regimes[index])
        next_regimes.append(next_regime)

    if not feature_rows:
        raise ValueError(
            f"Not enough usable history to build a regime dataset for {selected_pair}. "
            f"Need at least {required_history + 1} valid price points."
        )

    return RegimeDataset(
        pair_name=selected_pair,
        feature_names=feature_name_order,
        class_names=REGIME_CLASS_NAMES,
        X=np.asarray(feature_rows, dtype=float),
        y=np.asarray(labels, dtype=int),
        timestamps=tuple(timestamps),
        current_regimes=tuple(current_regimes),
        next_regimes=tuple(next_regimes),
    )


def train_regime_model(
    rows: Sequence[Any],
    *,
    pair_name: str | None = None,
    source: str = "in-memory rows",
    feature_config: FeatureConfig | None = None,
    training_config: RegimeTreeConfig | None = None,
    detector: RegimeDetector | None = None,
    model_dir: Path | str = DEFAULT_MODEL_DIR,
    model_name: str | None = None,
) -> RegimeTrainingResult:
    """Train, evaluate, and save a next-regime decision tree."""
    tree_cfg = training_config or RegimeTreeConfig()
    feat_cfg = feature_config or DEFAULT_CONFIG
    detector_ = detector or RegimeDetector()

    dataset = build_regime_dataset(
        rows,
        pair_name=pair_name,
        feature_config=feat_cfg,
        training_config=tree_cfg,
        detector=detector_,
        min_history=tree_cfg.min_history,
    )
    if dataset.sample_count < tree_cfg.min_samples:
        raise ValueError(
            f"Need at least {tree_cfg.min_samples} samples to train a model; "
            f"only found {dataset.sample_count}."
        )

    X_train, X_test, y_train, y_test = _chronological_train_test_split(
        dataset.X,
        dataset.y,
        test_fraction=tree_cfg.test_fraction,
    )
    model = RegimePredictor.fit(
        X_train,
        y_train,
        pair_name=dataset.pair_name,
        feature_names_=dataset.feature_names,
        class_names_=dataset.class_names,
        training_config=tree_cfg,
        feature_config=feat_cfg,
        detector=detector_,
    )
    train_metrics = model.evaluate(X_train, y_train)
    test_metrics = model.evaluate(X_test, y_test)
    model.metadata.update(
        {
            "source": source,
            "sample_count": dataset.sample_count,
            "train_metrics": asdict(train_metrics),
            "test_metrics": asdict(test_metrics),
        }
    )

    destination = _resolve_model_path(
        pair_name=dataset.pair_name,
        model_dir=model_dir,
        model_name=model_name,
    )
    saved_path = model.save(destination)
    return RegimeTrainingResult(
        pair_name=dataset.pair_name,
        source=source,
        sample_count=dataset.sample_count,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        model_path=saved_path,
        model=model,
    )


def train_regime_model_from_path(
    *,
    data_path: Path | str | None = None,
    pair_name: str | None = None,
    allow_synthetic: bool = False,
    feature_config: FeatureConfig | None = None,
    training_config: RegimeTreeConfig | None = None,
    detector: RegimeDetector | None = None,
    model_dir: Path | str = DEFAULT_MODEL_DIR,
    model_name: str | None = None,
) -> RegimeTrainingResult:
    """Load collected price data, then train, evaluate, and save a regime model."""
    from ..backtest import load_price_rows

    selected_path = Path(data_path) if data_path is not None else DATA_DIR
    rows, source = load_price_rows(selected_path)
    if source == "synthetic sample" and not allow_synthetic:
        raise ValueError(
            f"No collected price data was found in {selected_path}. "
            "Populate the data directory or pass allow_synthetic=True."
        )

    return train_regime_model(
        rows,
        pair_name=pair_name,
        source=source,
        feature_config=feature_config,
        training_config=training_config,
        detector=detector,
        model_dir=model_dir,
        model_name=model_name,
    )


def _grow_tree(
    X: np.ndarray,
    y: np.ndarray,
    *,
    depth: int,
    config: RegimeTreeConfig,
    class_count: int,
) -> _TreeNode:
    """Recursively build a simple CART-style classification tree."""
    class_counts = np.bincount(y, minlength=class_count).astype(int)
    node = _TreeNode(
        prediction_index=int(np.argmax(class_counts)),
        class_counts=class_counts.tolist(),
        sample_count=int(y.shape[0]),
        impurity=_gini_from_counts(class_counts),
    )
    if (
        depth >= config.max_depth
        or y.shape[0] < config.min_samples_split
        or node.impurity <= 0.0
    ):
        return node

    split = _best_split(
        X,
        y,
        class_count=class_count,
        min_samples_leaf=config.min_samples_leaf,
        parent_impurity=node.impurity,
    )
    if (
        split is None
        or split.impurity_gain <= config.min_impurity_decrease
    ):
        return node

    left_mask = X[:, split.feature_index] <= split.threshold
    right_mask = ~left_mask
    node.feature_index = split.feature_index
    node.threshold = split.threshold
    node.left = _grow_tree(
        X[left_mask],
        y[left_mask],
        depth=depth + 1,
        config=config,
        class_count=class_count,
    )
    node.right = _grow_tree(
        X[right_mask],
        y[right_mask],
        depth=depth + 1,
        config=config,
        class_count=class_count,
    )
    return node


def _best_split(
    X: np.ndarray,
    y: np.ndarray,
    *,
    class_count: int,
    min_samples_leaf: int,
    parent_impurity: float,
) -> _BestSplit | None:
    """Find the split with the best impurity reduction."""
    sample_count, feature_count = X.shape
    best: _BestSplit | None = None

    for feature_index in range(feature_count):
        feature_values = X[:, feature_index]
        order = np.argsort(feature_values, kind="mergesort")
        sorted_values = feature_values[order]
        sorted_labels = y[order]

        left_counts = np.zeros(class_count, dtype=int)
        right_counts = np.bincount(sorted_labels, minlength=class_count).astype(int)

        for split_index in range(sample_count - 1):
            label = int(sorted_labels[split_index])
            left_counts[label] += 1
            right_counts[label] -= 1

            left_size = split_index + 1
            right_size = sample_count - left_size
            if left_size < min_samples_leaf or right_size < min_samples_leaf:
                continue

            current_value = sorted_values[split_index]
            next_value = sorted_values[split_index + 1]
            if current_value == next_value:
                continue

            weighted_impurity = (
                (left_size / sample_count) * _gini_from_counts(left_counts)
                + (right_size / sample_count) * _gini_from_counts(right_counts)
            )
            impurity_gain = parent_impurity - weighted_impurity
            if best is None or impurity_gain > best.impurity_gain:
                best = _BestSplit(
                    feature_index=feature_index,
                    threshold=float((current_value + next_value) / 2.0),
                    impurity_gain=float(impurity_gain),
                )

    return best


def _gini_from_counts(counts: np.ndarray | Sequence[int]) -> float:
    """Return the Gini impurity represented by class counts."""
    total = float(sum(counts))
    if total <= 0:
        return 0.0
    probabilities = [(count / total) for count in counts if count > 0]
    return 1.0 - sum(probability * probability for probability in probabilities)


def _traverse_tree(node: _TreeNode, row: np.ndarray) -> _TreeNode:
    """Walk one feature row down the tree until reaching a leaf."""
    current = node
    while (
        not current.is_leaf
        and current.feature_index is not None
        and current.threshold is not None
    ):
        current = (
            current.left
            if row[current.feature_index] <= current.threshold
            else current.right
        )
        if current is None:
            break
    return current or node


def _tree_depth(node: _TreeNode | None) -> int:
    """Return the maximum depth of the tree."""
    if node is None or node.is_leaf:
        return 1 if node is not None else 0
    return 1 + max(_tree_depth(node.left), _tree_depth(node.right))


def _leaf_count(node: _TreeNode | None) -> int:
    """Return the number of leaf nodes in the tree."""
    if node is None:
        return 0
    if node.is_leaf:
        return 1
    return _leaf_count(node.left) + _leaf_count(node.right)


def _build_combined_feature_map(
    history: Sequence[PricePoint],
    regime_history: Sequence[str],
    *,
    feature_config: FeatureConfig,
    regime_lookback: int,
) -> dict[str, float]:
    """Merge numeric market features with regime-history features."""
    features = extract_features_from_history(history, config=feature_config)
    features.update(_build_regime_feature_map(regime_history, lookback=regime_lookback))
    return features


def _build_regime_feature_map(
    regime_history: Sequence[str],
    *,
    lookback: int,
) -> dict[str, float]:
    """Extract simple regime-history features for the decision tree."""
    if not regime_history:
        raise ValueError("regime_history must contain at least one label")

    recent = list(regime_history[-lookback:])
    current_regime = recent[-1]
    previous_regime = recent[-2] if len(recent) >= 2 else current_regime
    streak = 1
    for regime in reversed(recent[:-1]):
        if regime != current_regime:
            break
        streak += 1

    change_count = sum(
        1 for previous, current in zip(recent, recent[1:]) if previous != current
    )
    denominator = max(1, len(recent) - 1)

    features: dict[str, float] = {}
    for class_name in REGIME_CLASS_NAMES:
        label = class_name.lower()
        features[f"current_regime_{label}"] = (
            1.0 if current_regime == class_name else 0.0
        )
    for class_name in REGIME_CLASS_NAMES:
        label = class_name.lower()
        features[f"previous_regime_{label}"] = (
            1.0 if previous_regime == class_name else 0.0
        )
    features["regime_streak"] = float(streak)
    features["regime_change_rate"] = float(change_count / denominator)
    for class_name in REGIME_CLASS_NAMES:
        label = class_name.lower()
        features[f"regime_ratio_{label}"] = float(recent.count(class_name) / len(recent))
    return features


def _detect_regime_history(
    points: Sequence[PricePoint],
    detector: RegimeDetector,
) -> list[str]:
    """Return the detected regime for every history prefix."""
    return [
        detector.detect(_HistoryFeed(history=points[: index + 1])).value
        for index in range(len(points))
    ]


def _extract_pair_history(
    rows: Sequence[Any],
    pair_name: str,
) -> tuple[list[PricePoint], list[str]]:
    """Extract valid price points for one pair from historical rows."""
    points: list[PricePoint] = []
    timestamps: list[str] = []
    for index, row in enumerate(rows):
        price = _coerce_pair_price(row, pair_name)
        if price is None:
            continue
        timestamp_value = getattr(row, "timestamp", None)
        if timestamp_value is None and isinstance(row, dict):
            timestamp_value = row.get("timestamp")
        points.append(
            PricePoint(
                timestamp=_coerce_timestamp_seconds(timestamp_value, fallback=float(index)),
                price=price,
                source="historical",
            )
        )
        timestamps.append(_format_timestamp(timestamp_value if timestamp_value is not None else index))

    if len(points) < 2:
        raise ValueError(f"Not enough valid prices were found for {pair_name}")
    return points, timestamps


def _normalize_history_points(history: Iterable[Any]) -> list[PricePoint]:
    """Normalize price history into clean `PricePoint` instances."""
    points: list[PricePoint] = []
    for index, point in enumerate(history):
        price = _read_field(point, "price", default=0.0)
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric_price) or numeric_price <= 0:
            continue

        volume = _read_field(point, "volume_estimate", default=0.0)
        try:
            numeric_volume = float(volume)
        except (TypeError, ValueError):
            numeric_volume = 0.0
        if not math.isfinite(numeric_volume) or numeric_volume < 0:
            numeric_volume = 0.0

        timestamp_value = _read_field(point, "timestamp", default=index)
        source_value = _read_field(point, "source", default="historical")
        points.append(
            PricePoint(
                timestamp=_coerce_timestamp_seconds(
                    timestamp_value,
                    fallback=float(index),
                ),
                price=numeric_price,
                volume_estimate=numeric_volume,
                source=str(source_value or "historical"),
            )
        )
    return points


def _read_field(payload: Any, key: str, *, default: Any = None) -> Any:
    """Read an attribute or dictionary field with a fallback."""
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _infer_default_pair(rows: Sequence[Any]) -> str:
    """Choose the first tradable pair found in the loaded rows."""
    available_pairs: set[str] = set()
    for row in rows:
        prices = _row_prices(row)
        available_pairs.update(
            pair_name for pair_name in prices if pair_name != "SOL/USDC"
        )

    if not available_pairs:
        raise ValueError("No tradable pair was found in the provided rows")
    return sorted(available_pairs)[0]


def _row_prices(row: Any) -> dict[str, Any]:
    """Extract the nested price mapping from a row-like object."""
    prices = getattr(row, "prices", None)
    if prices is None and isinstance(row, dict):
        prices = row.get("prices")
    if not isinstance(prices, dict):
        raise TypeError("Rows must provide a .prices dictionary")
    return prices


def _coerce_pair_price(row: Any, pair_name: str) -> float | None:
    """Return a valid positive price for the requested pair or None."""
    raw_value = _row_prices(row).get(pair_name)
    if raw_value in (None, ""):
        return None
    value = float(raw_value)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _coerce_timestamp_seconds(value: Any, *, fallback: float) -> float:
    """Convert timestamp-like values into seconds for `PricePoint`."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).timestamp()
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return fallback


def _format_timestamp(value: Any) -> str:
    """Normalize timestamps for debugging metadata."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _ensure_matrix(
    values: np.ndarray | Sequence[float],
    *,
    expected_width: int | None = None,
) -> np.ndarray:
    """Normalize an array-like input into a 2-D feature matrix."""
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2:
        raise ValueError("Expected a 1-D feature row or a 2-D feature matrix")
    if expected_width is not None and matrix.shape[1] != expected_width:
        raise ValueError(
            f"Expected {expected_width} features, received {matrix.shape[1]}"
        )
    return matrix


def _ensure_label_vector(
    values: np.ndarray | Sequence[int],
    *,
    class_count: int,
) -> np.ndarray:
    """Normalize labels into a 1-D integer vector."""
    vector = np.asarray(values, dtype=int)
    if vector.ndim != 1:
        raise ValueError("Expected a 1-D label vector")
    if vector.size == 0:
        raise ValueError("At least one label is required")
    if np.any(vector < 0) or np.any(vector >= class_count):
        raise ValueError("Labels must be encoded between 0 and class_count - 1")
    return vector


def _multiclass_log_loss(labels: np.ndarray, probabilities: np.ndarray) -> float:
    """Return multiclass cross-entropy loss."""
    clipped = np.clip(probabilities, 1e-9, 1.0)
    row_indices = np.arange(labels.shape[0])
    return float(-np.mean(np.log(clipped[row_indices, labels])))


def _chronological_train_test_split(
    X: np.ndarray,
    y: np.ndarray,
    *,
    test_fraction: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split without shuffling to preserve time-series order."""
    sample_count = int(y.shape[0])
    if sample_count < 2:
        raise ValueError("At least two samples are required for train/test splitting")

    test_count = max(1, int(math.ceil(sample_count * test_fraction)))
    train_count = sample_count - test_count
    if train_count < 1:
        raise ValueError("test_fraction leaves no rows available for training")

    return (
        X[:train_count],
        X[train_count:],
        y[:train_count],
        y[train_count:],
    )


def _resolve_model_path(
    *,
    pair_name: str,
    model_dir: Path | str,
    model_name: str | None,
) -> Path:
    """Build the destination path for a saved model."""
    base_dir = Path(model_dir)
    if model_name:
        candidate = Path(model_name)
        return candidate if candidate.is_absolute() else base_dir / candidate
    normalized_pair = pair_name.lower().replace("/", "_")
    return base_dir / f"{normalized_pair}_regime_model.json"


def _detector_to_dict(detector: RegimeDetector) -> dict[str, Any]:
    """Serialize the detector configuration used for label generation."""
    return {
        "fast_window": int(detector.fast_window),
        "slow_window": int(detector.slow_window),
        "atr_window": int(detector.atr_window),
        "volatility_threshold": float(detector.volatility_threshold),
    }


def _detector_from_dict(payload: dict[str, Any] | None) -> RegimeDetector:
    """Restore a detector configuration or fall back to defaults."""
    config = payload or {}
    return RegimeDetector(
        fast_window=int(config.get("fast_window", 10)),
        slow_window=int(config.get("slow_window", 30)),
        atr_window=int(config.get("atr_window", 14)),
        volatility_threshold=float(config.get("volatility_threshold", 0.015)),
    )


def _build_parser() -> argparse.ArgumentParser:
    """Create a CLI parser for ad-hoc model training."""
    parser = argparse.ArgumentParser(
        description="Train a scratch decision tree regime predictor."
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DATA_DIR,
        help="Historical price file or directory. Defaults to ./data.",
    )
    parser.add_argument(
        "--pair",
        default=None,
        help="Pair to train on, such as JUP/USDC. Defaults to the first tradable pair found.",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help="Directory where the trained model JSON will be stored.",
    )
    parser.add_argument("--model-name", default=None, help="Optional output filename.")
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow training from the backtest synthetic sample if the data directory is empty.",
    )
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--min-samples-split", type=int, default=12)
    parser.add_argument("--min-samples-leaf", type=int, default=6)
    parser.add_argument("--min-impurity-decrease", type=float, default=1e-5)
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--min-samples", type=int, default=40)
    parser.add_argument("--min-history", type=int, default=None)
    parser.add_argument("--regime-lookback", type=int, default=12)
    parser.add_argument("--fast-window", type=int, default=10)
    parser.add_argument("--slow-window", type=int, default=30)
    parser.add_argument("--atr-window", type=int, default=14)
    parser.add_argument("--volatility-threshold", type=float, default=0.015)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    detector = RegimeDetector(
        fast_window=args.fast_window,
        slow_window=args.slow_window,
        atr_window=args.atr_window,
        volatility_threshold=args.volatility_threshold,
    )
    try:
        result = train_regime_model_from_path(
            data_path=args.data,
            pair_name=args.pair,
            allow_synthetic=args.allow_synthetic,
            training_config=RegimeTreeConfig(
                max_depth=args.max_depth,
                min_samples_split=args.min_samples_split,
                min_samples_leaf=args.min_samples_leaf,
                min_impurity_decrease=args.min_impurity_decrease,
                test_fraction=args.test_fraction,
                min_samples=args.min_samples,
                min_history=args.min_history,
                regime_lookback=args.regime_lookback,
            ),
            detector=detector,
            model_dir=args.model_dir,
            model_name=args.model_name,
        )
    except Exception as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"Pair: {result.pair_name}")
    print(f"Source: {result.source}")
    print(f"Samples: {result.sample_count}")
    print(f"Train accuracy: {result.train_metrics.accuracy:.3f}")
    print(f"Test accuracy: {result.test_metrics.accuracy:.3f}")
    print(f"Saved model: {result.model_path}")
    return 0


__all__ = [
    "DEFAULT_MODEL_DIR",
    "REGIME_CLASS_NAMES",
    "RegimeDataset",
    "RegimeEvaluationReport",
    "RegimePrediction",
    "RegimePredictor",
    "RegimeTrainingResult",
    "RegimeTreeConfig",
    "build_regime_dataset",
    "build_regime_feature_row_from_history",
    "main",
    "recommended_regime_min_history",
    "regime_feature_names",
    "train_regime_model",
    "train_regime_model_from_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
