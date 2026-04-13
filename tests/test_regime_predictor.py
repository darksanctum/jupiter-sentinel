from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from src.ml.feature_engineer import FeatureConfig, feature_names
from src.ml.regime_predictor import (
    REGIME_CLASS_NAMES,
    RegimePredictor,
    RegimeTreeConfig,
    build_regime_dataset,
    regime_feature_names,
    train_regime_model,
)
from src.oracle import PricePoint
from src.regime_detector import RegimeDetector


@dataclass(frozen=True)
class SyntheticRow:
    timestamp: datetime
    prices: dict[str, float]


def make_regime_rows(*, pair_name: str, cycles: int = 8) -> list[SyntheticRow]:
    start = datetime(2024, 1, 1, 0, 0, 0)
    price = 100.0
    rows: list[SyntheticRow] = []
    index = 0

    patterns = (
        ("bull", 18),
        ("sideways", 14),
        ("bear", 18),
        ("volatile", 14),
    )

    for _ in range(cycles):
        for regime_name, length in patterns:
            for step in range(length):
                rows.append(
                    SyntheticRow(
                        timestamp=start + timedelta(minutes=index * 30),
                        prices={"SOL/USDC": 100.0, pair_name: price},
                    )
                )
                if regime_name == "bull":
                    price *= 1.012
                elif regime_name == "bear":
                    price *= 0.988
                elif regime_name == "sideways":
                    price *= 1.001 if step % 2 == 0 else 0.999
                else:
                    price *= 1.05 if step % 2 == 0 else 0.95
                index += 1

    return rows


def test_build_regime_dataset_and_round_trip_training(tmp_path):
    pair_name = "TEST/USDC"
    rows = make_regime_rows(pair_name=pair_name)
    detector = RegimeDetector(
        fast_window=3,
        slow_window=6,
        atr_window=3,
        volatility_threshold=0.02,
    )
    feature_config = FeatureConfig(
        rsi_period=4,
        macd_fast_period=3,
        macd_slow_period=5,
        macd_signal_period=3,
        bollinger_period=5,
        volume_ratio_period=5,
        momentum_window=3,
        volatility_window=4,
        volatility_lookback=6,
        sma_periods=(3, 5, 8),
    )
    training_config = RegimeTreeConfig(
        max_depth=5,
        min_samples_split=10,
        min_samples_leaf=4,
        min_samples=40,
        regime_lookback=6,
    )

    dataset = build_regime_dataset(
        rows,
        pair_name=pair_name,
        feature_config=feature_config,
        training_config=training_config,
        detector=detector,
    )
    assert dataset.sample_count > 100
    assert dataset.X.shape[1] == len(feature_names(feature_config)) + len(
        regime_feature_names()
    )
    assert set(dataset.next_regimes) == set(REGIME_CLASS_NAMES)

    result = train_regime_model(
        rows,
        pair_name=pair_name,
        source="synthetic rows",
        feature_config=feature_config,
        training_config=training_config,
        detector=detector,
        model_dir=tmp_path,
    )

    assert result.model_path.exists()
    assert result.train_metrics.accuracy >= 0.70
    assert result.test_metrics.accuracy >= 0.55

    loaded_model = RegimePredictor.load(result.model_path)
    assert loaded_model.feature_names == result.model.feature_names
    assert loaded_model.predict(dataset.X[:8]).tolist() == result.model.predict(
        dataset.X[:8]
    ).tolist()

    predictions = loaded_model.predict_with_confidence(dataset.X[:8])
    assert len(predictions) == 8
    for prediction in predictions:
        assert prediction.regime in REGIME_CLASS_NAMES
        assert 0.0 <= prediction.confidence <= 1.0
        assert prediction.confidence == pytest.approx(
            max(prediction.probabilities.values())
        )
        assert sum(prediction.probabilities.values()) == pytest.approx(1.0)


def test_predict_next_regime_from_live_history_matches_feature_row_prediction(tmp_path):
    pair_name = "TEST/USDC"
    rows = make_regime_rows(pair_name=pair_name)
    detector = RegimeDetector(
        fast_window=3,
        slow_window=6,
        atr_window=3,
        volatility_threshold=0.02,
    )
    feature_config = FeatureConfig(
        rsi_period=4,
        macd_fast_period=3,
        macd_slow_period=5,
        macd_signal_period=3,
        bollinger_period=5,
        volume_ratio_period=5,
        momentum_window=3,
        volatility_window=4,
        volatility_lookback=6,
        sma_periods=(3, 5, 8),
    )
    training_config = RegimeTreeConfig(
        max_depth=5,
        min_samples_split=10,
        min_samples_leaf=4,
        min_samples=40,
        regime_lookback=6,
    )

    dataset = build_regime_dataset(
        rows,
        pair_name=pair_name,
        feature_config=feature_config,
        training_config=training_config,
        detector=detector,
    )
    result = train_regime_model(
        rows,
        pair_name=pair_name,
        source="synthetic rows",
        feature_config=feature_config,
        training_config=training_config,
        detector=detector,
        model_dir=tmp_path,
    )

    live_history = [
        PricePoint(
            timestamp=row.timestamp.timestamp(),
            price=row.prices[pair_name],
            source="historical",
        )
        for row in rows[:-1]
    ]

    from_history = result.model.predict_next_regime(live_history)
    from_matrix = result.model.predict_with_confidence(dataset.X[-1])[0]

    assert from_history.regime == from_matrix.regime
    assert from_history.confidence == pytest.approx(from_matrix.confidence)
    assert from_history.probabilities == pytest.approx(from_matrix.probabilities)
