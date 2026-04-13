from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import sys
import types

import pytest

from src.ml.feature_engineer import feature_names
from src.ml.predictor import (
    DirectionPredictor,
    build_direction_dataset,
    train_direction_model,
    train_direction_model_from_path,
)


@dataclass(frozen=True)
class SyntheticRow:
    timestamp: datetime
    prices: dict[str, float]


def make_rows(*, pair_name: str, steps: int = 240) -> list[SyntheticRow]:
    start = datetime(2024, 1, 1, 0, 0, 0)
    price = 100.0
    rows: list[SyntheticRow] = []

    for index in range(steps):
        rows.append(
            SyntheticRow(
                timestamp=start + timedelta(minutes=index * 30),
                prices={"SOL/USDC": 100.0, pair_name: price},
            )
        )
        direction = 1 if (index // 10) % 2 == 0 else -1
        price *= 1.0 + (0.01 * direction)

    return rows


def install_fake_state_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_state_manager = types.ModuleType("src.state_manager")

    class FakeStateManager:
        def __init__(self, *args, **kwargs):
            pass

        def get_locked_balance(self):
            return 0.0

    fake_state_manager.DEFAULT_LOCK_PCT = 0.5
    fake_state_manager.LOCK_PCT_ENV = "LOCK_PCT"
    fake_state_manager.StateManager = FakeStateManager
    monkeypatch.setitem(sys.modules, "src.state_manager", fake_state_manager)


def test_build_direction_dataset_and_round_trip_training(tmp_path):
    rows = make_rows(pair_name="TEST/USDC")

    dataset = build_direction_dataset(rows, pair_name="TEST/USDC")
    assert dataset.sample_count > 100
    assert dataset.X.shape[1] == len(feature_names())
    assert set(dataset.y.tolist()) == {0.0, 1.0}

    result = train_direction_model(
        rows,
        pair_name="TEST/USDC",
        source="synthetic rows",
        model_dir=tmp_path,
    )

    assert result.model_path.exists()
    assert result.train_metrics.accuracy >= 0.8
    assert result.test_metrics.accuracy >= 0.75

    loaded_model = DirectionPredictor.load(result.model_path)
    assert loaded_model.feature_names == result.model.feature_names
    assert loaded_model.predict(dataset.X[:8]).tolist() == result.model.predict(
        dataset.X[:8]
    ).tolist()


def test_train_direction_model_from_path_uses_collected_price_files(
    tmp_path, monkeypatch
):
    install_fake_state_manager(monkeypatch)

    data_dir = tmp_path / "data"
    model_dir = tmp_path / "models"
    data_dir.mkdir()

    rows = make_rows(pair_name="JUP/USDC", steps=220)
    sol_lines = ["timestamp,price"]
    jup_lines = ["timestamp,price"]
    for row in rows:
        timestamp = row.timestamp.isoformat()
        sol_lines.append(f"{timestamp},{row.prices['SOL/USDC']}")
        jup_lines.append(f"{timestamp},{row.prices['JUP/USDC']}")

    (data_dir / "sol_usdc.csv").write_text("\n".join(sol_lines), encoding="utf-8")
    (data_dir / "jup_usdc.csv").write_text("\n".join(jup_lines), encoding="utf-8")

    result = train_direction_model_from_path(
        data_path=data_dir,
        pair_name="JUP/USDC",
        model_dir=model_dir,
    )

    assert "2 files" in result.source
    assert result.model_path.parent == model_dir
    assert result.model_path.exists()
    assert result.test_metrics.sample_count > 0
    assert result.test_metrics.accuracy >= 0.75


def test_train_direction_model_from_path_rejects_empty_data_directory(
    tmp_path, monkeypatch
):
    install_fake_state_manager(monkeypatch)

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    with pytest.raises(ValueError, match="No collected price data was found"):
        train_direction_model_from_path(
            data_path=empty_dir,
            pair_name="JUP/USDC",
            model_dir=tmp_path / "models",
        )
