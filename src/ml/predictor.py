"""Simple next-period price direction predictor built with NumPy and SciPy."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np
try:
    from scipy.special import expit
except ModuleNotFoundError:  # pragma: no cover - exercised only when SciPy is absent.
    def expit(values: np.ndarray | Sequence[float]) -> np.ndarray:
        """Numerically stable sigmoid fallback when SciPy is unavailable."""
        clipped = np.clip(np.asarray(values, dtype=float), -500.0, 500.0)
        return 1.0 / (1.0 + np.exp(-clipped))

from ..config import DATA_DIR, SCAN_PAIRS
from ..oracle import PricePoint
from ..resilience import atomic_write_text
from .feature_engineer import (
    DEFAULT_CONFIG,
    FeatureConfig,
    extract_features_from_history,
    feature_names,
)

DEFAULT_MODEL_DIR = DATA_DIR / "models"


@dataclass(frozen=True)
class LogisticRegressionConfig:
    """Training configuration for the scratch logistic regression model."""

    learning_rate: float = 0.1
    epochs: int = 4000
    l2_strength: float = 1e-3
    test_fraction: float = 0.25
    classification_threshold: float = 0.5
    min_samples: int = 40
    min_history: int | None = None
    tolerance: float = 1e-7
    patience: int = 150

    def __post_init__(self) -> None:
        if not math.isfinite(self.learning_rate) or self.learning_rate <= 0:
            raise ValueError("learning_rate must be a finite float > 0")
        if not isinstance(self.epochs, int) or self.epochs < 1:
            raise ValueError("epochs must be an integer >= 1")
        if not math.isfinite(self.l2_strength) or self.l2_strength < 0:
            raise ValueError("l2_strength must be a finite float >= 0")
        if not math.isfinite(self.test_fraction) or not 0 < self.test_fraction < 1:
            raise ValueError("test_fraction must be a finite float between 0 and 1")
        if (
            not math.isfinite(self.classification_threshold)
            or not 0 < self.classification_threshold < 1
        ):
            raise ValueError(
                "classification_threshold must be a finite float between 0 and 1"
            )
        if not isinstance(self.min_samples, int) or self.min_samples < 2:
            raise ValueError("min_samples must be an integer >= 2")
        if self.min_history is not None and (
            not isinstance(self.min_history, int) or self.min_history < 2
        ):
            raise ValueError("min_history must be None or an integer >= 2")
        if not math.isfinite(self.tolerance) or self.tolerance < 0:
            raise ValueError("tolerance must be a finite float >= 0")
        if not isinstance(self.patience, int) or self.patience < 1:
            raise ValueError("patience must be an integer >= 1")


@dataclass(frozen=True)
class EvaluationReport:
    """Basic evaluation metrics for a binary classifier."""

    accuracy: float
    log_loss: float
    sample_count: int
    positive_rate: float
    predicted_positive_rate: float


@dataclass(frozen=True)
class DirectionDataset:
    """Feature matrix and labels for next-period direction prediction."""

    pair_name: str
    feature_names: tuple[str, ...]
    X: np.ndarray
    y: np.ndarray
    timestamps: tuple[str, ...]
    current_prices: np.ndarray
    next_prices: np.ndarray

    @property
    def sample_count(self) -> int:
        """Return the number of supervised samples."""
        return int(self.y.shape[0])


@dataclass(frozen=True)
class TrainingResult:
    """Structured output from a train/evaluate/save run."""

    pair_name: str
    source: str
    sample_count: int
    train_metrics: EvaluationReport
    test_metrics: EvaluationReport
    model_path: Path
    model: "DirectionPredictor"


@dataclass
class DirectionPredictor:
    """A lightweight logistic regression model for price direction."""

    pair_name: str
    feature_names: tuple[str, ...]
    weights: np.ndarray
    bias: float
    feature_means: np.ndarray
    feature_scales: np.ndarray
    training_config: LogisticRegressionConfig = field(
        default_factory=LogisticRegressionConfig
    )
    feature_config: FeatureConfig = field(default_factory=lambda: DEFAULT_CONFIG)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected_size = len(self.feature_names)
        self.weights = np.asarray(self.weights, dtype=float)
        self.feature_means = np.asarray(self.feature_means, dtype=float)
        self.feature_scales = np.asarray(self.feature_scales, dtype=float)
        self.bias = float(self.bias)

        if self.weights.shape != (expected_size,):
            raise ValueError("weights must align with feature_names")
        if self.feature_means.shape != (expected_size,):
            raise ValueError("feature_means must align with feature_names")
        if self.feature_scales.shape != (expected_size,):
            raise ValueError("feature_scales must align with feature_names")

    @classmethod
    def fit(
        cls,
        X: np.ndarray,
        y: np.ndarray,
        *,
        pair_name: str,
        feature_names_: Sequence[str],
        training_config: LogisticRegressionConfig | None = None,
        feature_config: FeatureConfig | None = None,
    ) -> "DirectionPredictor":
        """Fit a scratch logistic regression model with gradient descent."""
        cfg = training_config or LogisticRegressionConfig()
        feat_cfg = feature_config or DEFAULT_CONFIG

        matrix = _ensure_matrix(X)
        labels = _ensure_binary_vector(y)
        if matrix.shape[0] != labels.shape[0]:
            raise ValueError("X and y must contain the same number of rows")
        if matrix.shape[0] < 2:
            raise ValueError("At least two training rows are required")

        feature_means = matrix.mean(axis=0)
        feature_scales = matrix.std(axis=0)
        feature_scales = np.where(feature_scales > 1e-12, feature_scales, 1.0)
        normalized = (matrix - feature_means) / feature_scales

        weights = np.zeros(normalized.shape[1], dtype=float)
        bias = 0.0
        best_weights = weights.copy()
        best_bias = bias
        best_loss = math.inf
        epochs_without_improvement = 0

        for epoch in range(cfg.epochs):
            logits = normalized @ weights + bias
            probabilities = expit(logits)
            errors = probabilities - labels

            gradient_w = (normalized.T @ errors) / labels.shape[0]
            if cfg.l2_strength:
                gradient_w = gradient_w + (cfg.l2_strength * weights)
            gradient_b = float(np.mean(errors))

            weights = weights - (cfg.learning_rate * gradient_w)
            bias = bias - (cfg.learning_rate * gradient_b)

            updated_probabilities = expit((normalized @ weights) + bias)
            loss = _binary_log_loss(labels, updated_probabilities)
            if cfg.l2_strength:
                loss += 0.5 * cfg.l2_strength * float(np.dot(weights, weights))

            if best_loss - loss > cfg.tolerance:
                best_loss = loss
                best_weights = weights.copy()
                best_bias = bias
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
                if epochs_without_improvement >= cfg.patience:
                    break

        return cls(
            pair_name=pair_name,
            feature_names=tuple(feature_names_),
            weights=best_weights,
            bias=best_bias,
            feature_means=feature_means,
            feature_scales=feature_scales,
            training_config=cfg,
            feature_config=feat_cfg,
            metadata={
                "training_loss": best_loss,
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "epochs_configured": cfg.epochs,
            },
        )

    def predict_proba(self, X: np.ndarray | Sequence[float]) -> np.ndarray:
        """Predict the probability that the next price move is up."""
        matrix = _ensure_matrix(X, expected_width=len(self.feature_names))
        normalized = (matrix - self.feature_means) / self.feature_scales
        return expit((normalized @ self.weights) + self.bias)

    def predict(self, X: np.ndarray | Sequence[float]) -> np.ndarray:
        """Predict binary up/down labels."""
        probabilities = self.predict_proba(X)
        return (probabilities >= self.training_config.classification_threshold).astype(
            int
        )

    def evaluate(self, X: np.ndarray, y: np.ndarray) -> EvaluationReport:
        """Evaluate the model on a feature matrix and label vector."""
        labels = _ensure_binary_vector(y)
        probabilities = self.predict_proba(X)
        predictions = (
            probabilities >= self.training_config.classification_threshold
        ).astype(int)
        accuracy = float(np.mean(predictions == labels)) if labels.size else 0.0
        return EvaluationReport(
            accuracy=accuracy,
            log_loss=_binary_log_loss(labels, probabilities),
            sample_count=int(labels.shape[0]),
            positive_rate=float(np.mean(labels)) if labels.size else 0.0,
            predicted_positive_rate=float(np.mean(predictions))
            if predictions.size
            else 0.0,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the trained model to a JSON-compatible payload."""
        return {
            "model_type": "logistic_regression_direction",
            "version": 1,
            "pair_name": self.pair_name,
            "feature_names": list(self.feature_names),
            "weights": self.weights.tolist(),
            "bias": self.bias,
            "feature_means": self.feature_means.tolist(),
            "feature_scales": self.feature_scales.tolist(),
            "training_config": asdict(self.training_config),
            "feature_config": asdict(self.feature_config),
            "metadata": self.metadata,
        }

    def save(self, path: Path | str) -> Path:
        """Persist the model as JSON."""
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        return atomic_write_text(path, payload, encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "DirectionPredictor":
        """Restore a model from JSON."""
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)

        return cls(
            pair_name=payload["pair_name"],
            feature_names=tuple(payload["feature_names"]),
            weights=np.asarray(payload["weights"], dtype=float),
            bias=float(payload["bias"]),
            feature_means=np.asarray(payload["feature_means"], dtype=float),
            feature_scales=np.asarray(payload["feature_scales"], dtype=float),
            training_config=LogisticRegressionConfig(**payload["training_config"]),
            feature_config=FeatureConfig(**payload["feature_config"]),
            metadata=dict(payload.get("metadata", {})),
        )


def recommended_min_history(config: FeatureConfig | None = None) -> int:
    """Return a conservative minimum history length for stable features."""
    cfg = config or DEFAULT_CONFIG
    return max(
        max(cfg.sma_periods),
        cfg.rsi_period + 1,
        cfg.macd_slow_period + cfg.macd_signal_period,
        cfg.bollinger_period,
        cfg.volume_ratio_period + 1,
        cfg.momentum_window + 1,
        cfg.volatility_window + cfg.volatility_lookback,
    )


def infer_default_pair(rows: Sequence[Any]) -> str:
    """Choose a tradable pair from the loaded price rows."""
    available_pairs = set()
    for row in rows:
        available_pairs.update(_row_prices(row))

    for _, _, pair_name in SCAN_PAIRS:
        if pair_name != "SOL/USDC" and pair_name in available_pairs:
            return pair_name

    fallback = sorted(pair_name for pair_name in available_pairs if pair_name != "SOL/USDC")
    if fallback:
        return fallback[0]
    raise ValueError("No tradable pair was found in the provided rows")


def build_direction_dataset(
    rows: Sequence[Any],
    *,
    pair_name: str | None = None,
    feature_config: FeatureConfig | None = None,
    min_history: int | None = None,
) -> DirectionDataset:
    """Build a supervised dataset for next-period direction prediction."""
    ordered_rows = list(rows)
    if len(ordered_rows) < 2:
        raise ValueError("At least two rows are required to build a direction dataset")

    feat_cfg = feature_config or DEFAULT_CONFIG
    selected_pair = pair_name or infer_default_pair(ordered_rows)
    required_history = (
        recommended_min_history(feat_cfg) if min_history is None else min_history
    )
    ordered_feature_names = tuple(feature_names(feat_cfg))

    history: list[PricePoint] = []
    feature_rows: list[list[float]] = []
    labels: list[float] = []
    timestamps: list[str] = []
    current_prices: list[float] = []
    next_prices: list[float] = []

    for index in range(len(ordered_rows) - 1):
        current_row = ordered_rows[index]
        next_row = ordered_rows[index + 1]

        current_price = _coerce_pair_price(current_row, selected_pair)
        next_price = _coerce_pair_price(next_row, selected_pair)
        if current_price is None or next_price is None:
            continue

        history.append(
            PricePoint(
                timestamp=_coerce_timestamp_seconds(
                    getattr(current_row, "timestamp", index),
                    fallback=float(index),
                ),
                price=current_price,
                source="historical",
            )
        )
        if len(history) < required_history:
            continue

        feature_map = extract_features_from_history(history, config=feat_cfg)
        feature_rows.append([float(feature_map[name]) for name in ordered_feature_names])
        labels.append(1.0 if next_price > current_price else 0.0)
        timestamps.append(_format_timestamp(getattr(current_row, "timestamp", index)))
        current_prices.append(current_price)
        next_prices.append(next_price)

    if not feature_rows:
        raise ValueError(
            f"Not enough usable history to build a dataset for {selected_pair}. "
            f"Need at least {required_history} valid price points."
        )

    return DirectionDataset(
        pair_name=selected_pair,
        feature_names=ordered_feature_names,
        X=np.asarray(feature_rows, dtype=float),
        y=np.asarray(labels, dtype=float),
        timestamps=tuple(timestamps),
        current_prices=np.asarray(current_prices, dtype=float),
        next_prices=np.asarray(next_prices, dtype=float),
    )


def train_direction_model(
    rows: Sequence[Any],
    *,
    pair_name: str | None = None,
    source: str = "in-memory rows",
    feature_config: FeatureConfig | None = None,
    training_config: LogisticRegressionConfig | None = None,
    model_dir: Path | str = DEFAULT_MODEL_DIR,
    model_name: str | None = None,
) -> TrainingResult:
    """Train, evaluate, and save a direction model from loaded price rows."""
    cfg = training_config or LogisticRegressionConfig()
    feat_cfg = feature_config or DEFAULT_CONFIG

    dataset = build_direction_dataset(
        rows,
        pair_name=pair_name,
        feature_config=feat_cfg,
        min_history=cfg.min_history,
    )
    if dataset.sample_count < cfg.min_samples:
        raise ValueError(
            f"Need at least {cfg.min_samples} samples to train a model; "
            f"only found {dataset.sample_count}."
        )

    X_train, X_test, y_train, y_test = _chronological_train_test_split(
        dataset.X,
        dataset.y,
        test_fraction=cfg.test_fraction,
    )
    model = DirectionPredictor.fit(
        X_train,
        y_train,
        pair_name=dataset.pair_name,
        feature_names_=dataset.feature_names,
        training_config=cfg,
        feature_config=feat_cfg,
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
    return TrainingResult(
        pair_name=dataset.pair_name,
        source=source,
        sample_count=dataset.sample_count,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        model_path=saved_path,
        model=model,
    )


def train_direction_model_from_path(
    *,
    data_path: Path | str | None = None,
    pair_name: str | None = None,
    allow_synthetic: bool = False,
    feature_config: FeatureConfig | None = None,
    training_config: LogisticRegressionConfig | None = None,
    model_dir: Path | str = DEFAULT_MODEL_DIR,
    model_name: str | None = None,
) -> TrainingResult:
    """Load collected price data, then train, evaluate, and save a model."""
    from ..backtest import load_price_rows

    selected_path = Path(data_path) if data_path is not None else DATA_DIR
    rows, source = load_price_rows(selected_path)
    if source == "synthetic sample" and not allow_synthetic:
        raise ValueError(
            f"No collected price data was found in {selected_path}. "
            "Populate the data directory or pass allow_synthetic=True."
        )

    return train_direction_model(
        rows,
        pair_name=pair_name,
        source=source,
        feature_config=feature_config,
        training_config=training_config,
        model_dir=model_dir,
        model_name=model_name,
    )


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


def _ensure_binary_vector(values: np.ndarray | Sequence[float]) -> np.ndarray:
    """Normalize labels into a 1-D float vector."""
    vector = np.asarray(values, dtype=float)
    if vector.ndim != 1:
        raise ValueError("Expected a 1-D label vector")
    if vector.size == 0:
        raise ValueError("At least one label is required")
    if not np.all(np.isin(vector, [0.0, 1.0])):
        raise ValueError("Labels must be binary values encoded as 0/1")
    return vector


def _binary_log_loss(labels: np.ndarray, probabilities: np.ndarray) -> float:
    """Return binary cross-entropy loss."""
    clipped = np.clip(probabilities, 1e-9, 1.0 - 1e-9)
    return float(-np.mean((labels * np.log(clipped)) + ((1.0 - labels) * np.log(1.0 - clipped))))


def _chronological_train_test_split(
    X: np.ndarray,
    y: np.ndarray,
    *,
    test_fraction: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split without shuffling to preserve the time-series order."""
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
    return base_dir / f"{normalized_pair}_direction_model.json"


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


def _build_parser() -> argparse.ArgumentParser:
    """Create a CLI parser for ad-hoc model training."""
    parser = argparse.ArgumentParser(
        description="Train a scratch logistic regression price-direction predictor."
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
    parser.add_argument("--learning-rate", type=float, default=0.1)
    parser.add_argument("--epochs", type=int, default=4000)
    parser.add_argument("--l2-strength", type=float, default=1e-3)
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--min-samples", type=int, default=40)
    parser.add_argument("--min-history", type=int, default=None)
    parser.add_argument("--patience", type=int, default=150)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        result = train_direction_model_from_path(
            data_path=args.data,
            pair_name=args.pair,
            allow_synthetic=args.allow_synthetic,
            training_config=LogisticRegressionConfig(
                learning_rate=args.learning_rate,
                epochs=args.epochs,
                l2_strength=args.l2_strength,
                test_fraction=args.test_fraction,
                min_samples=args.min_samples,
                min_history=args.min_history,
                patience=args.patience,
            ),
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
    "DirectionDataset",
    "DirectionPredictor",
    "EvaluationReport",
    "LogisticRegressionConfig",
    "TrainingResult",
    "build_direction_dataset",
    "infer_default_pair",
    "main",
    "recommended_min_history",
    "train_direction_model",
    "train_direction_model_from_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
