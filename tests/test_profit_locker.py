import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.profit_locker as profit_locker
from src.state_manager import StateManager


@pytest.fixture
def profit_lock_path(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(profit_locker, "PROFIT_LOCK_PATH", path)
    return path


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_get_locked_balance_defaults_to_zero_without_creating_file(profit_lock_path):
    assert profit_locker.get_locked_balance() == pytest.approx(0.0)
    assert not profit_lock_path.exists()


def test_winning_trade_locks_half_the_profit_and_reduces_tradable_balance(profit_lock_path):
    locked_amount = profit_locker.lock_profit(2.0)

    assert locked_amount == pytest.approx(1.0)
    assert profit_locker.get_locked_balance() == pytest.approx(1.0)
    assert profit_locker.get_tradable_balance(5.0) == pytest.approx(4.0)

    payload = read_state(profit_lock_path)
    assert payload["locked_balance"] == pytest.approx(1.0)
    assert payload["profit_tracking"]["locked_profit_sol"] == pytest.approx(1.0)


def test_zero_and_negative_profit_never_decrease_locked_balance(profit_lock_path):
    balance_history = [profit_locker.get_locked_balance()]

    balance_history.append(profit_locker.lock_profit(2.0))
    balance_history.append(profit_locker.get_locked_balance())

    assert profit_locker.lock_profit(0.0) == pytest.approx(0.0)
    balance_history.append(profit_locker.get_locked_balance())

    assert profit_locker.lock_profit(-3.0) == pytest.approx(0.0)
    balance_history.append(profit_locker.get_locked_balance())

    assert profit_locker.lock_profit(1.0) == pytest.approx(0.5)
    balance_history.append(profit_locker.get_locked_balance())

    assert balance_history == pytest.approx([0.0, 1.0, 1.0, 1.0, 1.0, 1.5])
    assert balance_history == sorted(balance_history)
    assert read_state(profit_lock_path)["locked_balance"] == pytest.approx(1.5)


def test_multiple_quick_trades_accumulate_locked_balance_without_regressions(profit_lock_path):
    profits = [0.8, 1.2, 0.4, 1.6]
    expected_locked_amounts = [0.4, 0.6, 0.2, 0.8]
    running_balances = []

    for profit, expected_locked in zip(profits, expected_locked_amounts):
        assert profit_locker.lock_profit(profit) == pytest.approx(expected_locked)
        running_balances.append(profit_locker.get_locked_balance())

    assert running_balances == pytest.approx([0.4, 1.0, 1.2, 2.0])
    assert running_balances == sorted(running_balances)
    assert profit_locker.get_locked_balance() == pytest.approx(2.0)
    assert profit_locker.get_tradable_balance(10.0) == pytest.approx(8.0)


def test_locked_balance_and_tradable_balance_persist_across_restarts(profit_lock_path):
    profit_locker.lock_profit(2.0)
    first_restart = StateManager(profit_lock_path)

    assert first_restart.get_locked_balance() == pytest.approx(1.0)
    assert profit_locker.get_tradable_balance(3.0) == pytest.approx(2.0)

    first_restart.lock_profit(4.0)
    second_restart = StateManager(profit_lock_path)

    assert second_restart.get_locked_balance() == pytest.approx(3.0)
    assert profit_locker.get_locked_balance() == pytest.approx(3.0)
    assert profit_locker.get_tradable_balance(10.0) == pytest.approx(7.0)


def test_get_tradable_balance_uses_executor_when_balance_is_omitted(profit_lock_path):
    profit_locker.lock_profit(2.0)

    class FakeExecutor:
        def get_balance(self):
            return {"sol": 3.0}

    assert profit_locker.get_tradable_balance(executor=FakeExecutor()) == pytest.approx(2.0)
    assert profit_locker.get_tradable_balance(1.0) == pytest.approx(0.0)


def test_lock_profit_honors_configured_percentage(profit_lock_path, monkeypatch):
    monkeypatch.setenv("PROFIT_LOCK_PCT", "0.25")

    locked_amount = profit_locker.lock_profit(4.0)

    assert locked_amount == pytest.approx(1.0)
    assert profit_locker.get_locked_balance() == pytest.approx(1.0)
