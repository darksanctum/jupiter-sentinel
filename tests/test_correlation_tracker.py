import json
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import BONK_MINT, JUP_MINT, SOL_MINT, USDC_MINT
from src.correlation_tracker import CorrelationTracker
from src.oracle import PricePoint


class FakeFeed:
    def __init__(self, prices):
        self.history = deque(
            [
                PricePoint(timestamp=float(index), price=float(price))
                for index, price in enumerate(prices, start=1)
            ],
            maxlen=60,
        )


def test_refresh_persists_matrix_using_canonical_watchlist_pairs(tmp_path):
    tracker = CorrelationTracker(path=tmp_path / "correlations.json")
    feeds = {
        "JUP/USDC": FakeFeed([1.0, 1.1, 0.99, 1.188]),
        "JUP/SOL": FakeFeed([1.0, 0.9, 0.99, 0.792]),
        "BONK/USDC": FakeFeed([2.0, 2.2, 1.98, 2.376]),
    }

    assert tracker.refresh_if_due(feeds, force=True) is True

    payload = json.loads((tmp_path / "correlations.json").read_text(encoding="utf-8"))
    assert payload["tokens"][JUP_MINT]["pair"] == "JUP/USDC"
    assert payload["matrix"][JUP_MINT][BONK_MINT] == pytest.approx(1.0)
    assert payload["matrix"][BONK_MINT][JUP_MINT] == pytest.approx(1.0)


def test_refresh_waits_for_hour_boundary_before_recomputing(tmp_path):
    now = {"value": 0.0}
    tracker = CorrelationTracker(
        path=tmp_path / "correlations.json",
        refresh_interval_seconds=3600.0,
        time_fn=lambda: now["value"],
    )
    feeds = {
        "JUP/USDC": FakeFeed([1.0, 1.1, 0.99, 1.188]),
        "BONK/USDC": FakeFeed([2.0, 2.2, 1.98, 2.376]),
    }

    assert tracker.refresh_if_due(feeds, force=True) is True
    first_matrix = json.loads((tmp_path / "correlations.json").read_text(encoding="utf-8"))[
        "matrix"
    ]

    now["value"] = 3599.0
    feeds["BONK/USDC"] = FakeFeed([2.0, 1.8, 1.98, 1.584])

    assert tracker.refresh_if_due(feeds) is False

    second_matrix = json.loads((tmp_path / "correlations.json").read_text(encoding="utf-8"))[
        "matrix"
    ]
    assert second_matrix == first_matrix


def test_find_correlated_open_position_uses_token_matrix_for_conflict_detection(
    tmp_path,
):
    tracker = CorrelationTracker(path=tmp_path / "correlations.json")
    tracker.refresh_if_due(
        {
            "JUP/USDC": FakeFeed([1.0, 1.1, 0.99, 1.188]),
            "BONK/USDC": FakeFeed([2.0, 2.2, 1.98, 2.376]),
        },
        force=True,
    )

    conflict = tracker.find_correlated_open_position(
        "BONK/USDC",
        BONK_MINT,
        USDC_MINT,
        [
            {
                "pair": "JUP/USDC",
                "input_mint": JUP_MINT,
                "output_mint": USDC_MINT,
                "status": "open",
            }
        ],
    )

    assert conflict is not None
    assert conflict["candidate_symbol"] == "BONK"
    assert conflict["open_symbol"] == "JUP"
    assert conflict["correlation"] == pytest.approx(1.0)
