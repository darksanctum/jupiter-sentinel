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


def __getattr__(name: str):
    """Lazily load predictor exports so `python -m src.ml.predictor` stays clean."""
    if name in _PREDICTOR_EXPORTS:
        module = import_module(".predictor", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_MODEL_DIR",
    "DirectionDataset",
    "DirectionPredictor",
    "EvaluationReport",
    "FeatureConfig",
    "LogisticRegressionConfig",
    "TrainingResult",
    "build_direction_dataset",
    "extract_features",
    "extract_features_batch",
    "extract_features_from_history",
    "feature_names",
    "infer_default_pair",
    "recommended_min_history",
    "train_direction_model",
    "train_direction_model_from_path",
]
