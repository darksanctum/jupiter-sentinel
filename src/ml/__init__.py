"""ML utilities built on top of Jupiter Sentinel price history."""

from importlib import import_module

from .feature_engineer import (
    DEFAULT_CONFIG,
    FeatureConfig,
    extract_features,
    extract_features_batch,
    extract_features_from_history,
    feature_names,
)
from .anomaly_detector import (
    DEFAULT_ANOMALY_CONFIG,
    AnomalyConfig,
    AnomalyDetector,
    AnomalyKind,
    AnomalySignal,
    MetricSnapshot,
    detect_anomaly,
    detect_anomaly_from_history,
)

_PREDICTOR_EXPORTS = {
    "DEFAULT_MODEL_DIR",
    "DirectionDataset",
    "DirectionPredictor",
    "EvaluationReport",
    "LogisticRegressionConfig",
    "TrainingResult",
    "build_direction_dataset",
    "infer_default_pair",
    "recommended_min_history",
    "train_direction_model",
    "train_direction_model_from_path",
}

_REGIME_PREDICTOR_EXPORTS = {
    "REGIME_CLASS_NAMES",
    "RegimeDataset",
    "RegimeEvaluationReport",
    "RegimePrediction",
    "RegimePredictor",
    "RegimeTrainingResult",
    "RegimeTreeConfig",
    "build_regime_dataset",
    "build_regime_feature_row_from_history",
    "recommended_regime_min_history",
    "regime_feature_names",
    "train_regime_model",
    "train_regime_model_from_path",
}


def __getattr__(name: str):
    """Lazily load predictor exports so `python -m src.ml.predictor` stays clean."""
    if name in _PREDICTOR_EXPORTS:
        module = import_module(".predictor", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name in _REGIME_PREDICTOR_EXPORTS:
        module = import_module(".regime_predictor", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_ANOMALY_CONFIG",
    "DEFAULT_MODEL_DIR",
    "AnomalyConfig",
    "AnomalyDetector",
    "AnomalyKind",
    "AnomalySignal",
    "DirectionDataset",
    "DirectionPredictor",
    "EvaluationReport",
    "FeatureConfig",
    "LogisticRegressionConfig",
    "MetricSnapshot",
    "TrainingResult",
    "build_direction_dataset",
    "detect_anomaly",
    "detect_anomaly_from_history",
    "extract_features",
    "extract_features_batch",
    "extract_features_from_history",
    "feature_names",
    "infer_default_pair",
    "REGIME_CLASS_NAMES",
    "RegimeDataset",
    "RegimeEvaluationReport",
    "RegimePrediction",
    "RegimePredictor",
    "RegimeTrainingResult",
    "RegimeTreeConfig",
    "build_regime_dataset",
    "build_regime_feature_row_from_history",
    "recommended_min_history",
    "recommended_regime_min_history",
    "regime_feature_names",
    "train_direction_model",
    "train_direction_model_from_path",
    "train_regime_model",
    "train_regime_model_from_path",
]
