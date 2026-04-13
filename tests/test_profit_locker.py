import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.profit_locker as profit_locker


def test_get_locked_balance_defaults_to_zero_without_creating_file(tmp_path, monkeypatch):
    path = tmp_path / "profits.json"
    monkeypatch.setattr(profit_locker, "PROFIT_LOCK_PATH", path)

    assert profit_locker.get_locked_balance() == pytest.approx(0.0)
    assert not path.exists()


def test_lock_profit_persists_default_half_of_realized_profit(tmp_path, monkeypatch):
    path = tmp_path / "profits.json"
    monkeypatch.setattr(profit_locker, "PROFIT_LOCK_PATH", path)

    locked_amount = profit_locker.lock_profit(2.0)

    assert locked_amount == pytest.approx(1.0)
    assert profit_locker.get_locked_balance() == pytest.approx(1.0)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["locked_balance"] == pytest.approx(1.0)


def test_lock_profit_honors_configured_percentage(tmp_path, monkeypatch):
    path = tmp_path / "profits.json"
    monkeypatch.setattr(profit_locker, "PROFIT_LOCK_PATH", path)
    monkeypatch.setenv("PROFIT_LOCK_PCT", "0.25")

    locked_amount = profit_locker.lock_profit(4.0)

    assert locked_amount == pytest.approx(1.0)
    assert profit_locker.get_locked_balance() == pytest.approx(1.0)


def test_get_tradable_balance_subtracts_locked_balance_and_never_goes_negative(tmp_path, monkeypatch):
    path = tmp_path / "profits.json"
    monkeypatch.setattr(profit_locker, "PROFIT_LOCK_PATH", path)
    profit_locker.lock_profit(4.0)

    assert profit_locker.get_tradable_balance(10.0) == pytest.approx(8.0)
    assert profit_locker.get_tradable_balance(1.0) == pytest.approx(0.0)


def test_get_tradable_balance_uses_executor_when_balance_is_omitted(tmp_path, monkeypatch):
    path = tmp_path / "profits.json"
    monkeypatch.setattr(profit_locker, "PROFIT_LOCK_PATH", path)
    profit_locker.lock_profit(2.0)

    class FakeExecutor:
        def get_balance(self):
            return {"sol": 3.0}

    assert profit_locker.get_tradable_balance(executor=FakeExecutor()) == pytest.approx(2.0)
