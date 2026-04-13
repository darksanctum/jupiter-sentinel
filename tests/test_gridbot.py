import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

fake_state_manager = types.ModuleType("src.state_manager")


class FakeStateManager:
    def __init__(self, *args, **kwargs):
        pass


fake_state_manager.DEFAULT_LOCK_PCT = 0.5
fake_state_manager.LOCK_PCT_ENV = "LOCK_PCT"
fake_state_manager.StateManager = FakeStateManager
sys.modules.setdefault("src.state_manager", fake_state_manager)

from src.config import SOL_MINT, USDC_MINT
from src.gridbot import GridBot


class FakeExecutor:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.pubkey = "test-wallet"

    def execute_swap(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Unexpected execute_swap call")
        return self.responses.pop(0)

    def get_balance(self):
        return {"sol": 0.0, "address": self.pubkey}


def test_create_grid_places_buys_below_and_sells_above_and_persists(tmp_path):
    state_path = tmp_path / "grid_state.json"
    bot = GridBot(num_levels=2, amount_per_level_sol=1.0, executor=FakeExecutor(), state_path=state_path)

    grid = bot.create_grid(
        "SOL/USDC",
        SOL_MINT,
        USDC_MINT,
        current_price=100.0,
        base_balance=3.0,
        quote_balance=500.0,
    )

    assert grid is not None
    assert grid.grid_spacing_pct == pytest.approx(2.0)
    assert len(grid.levels) == 4

    buy_levels = sorted((level for level in grid.levels if level.side == "buy"), key=lambda level: level.price)
    sell_levels = sorted((level for level in grid.levels if level.side == "sell"), key=lambda level: level.price)

    assert all(level.price < 100.0 for level in buy_levels)
    assert all(level.price > 100.0 for level in sell_levels)
    assert sell_levels[0].price == pytest.approx(102.0)
    assert buy_levels[-1].price == pytest.approx(100.0 / 1.02)
    assert all(level.side in {"buy", "sell"} for level in grid.levels)

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["grids"][0]["pair"] == "SOL/USDC"
    assert payload["grids"][0]["center_price"] == pytest.approx(100.0)


def test_check_grid_executes_buy_and_flips_level_to_sell(tmp_path):
    state_path = tmp_path / "grid_state.json"
    executor = FakeExecutor(
        responses=[
            {
                "status": "dry_run",
                "out_amount": 1_000_000_000,
            }
        ]
    )
    bot = GridBot(num_levels=2, amount_per_level_sol=1.0, executor=executor, state_path=state_path)
    grid = bot.create_grid(
        "SOL/USDC",
        SOL_MINT,
        USDC_MINT,
        current_price=100.0,
        base_balance=0.0,
        quote_balance=250.0,
    )

    actions = bot.check_grid(grid, current_price=98.0, dry_run=True)

    assert len(actions) == 1
    assert actions[0]["action"] == "BUY"
    assert actions[0]["status"] == "dry_run"
    assert executor.calls == [
        {
            "input_mint": USDC_MINT,
            "output_mint": SOL_MINT,
            "amount": 98_039_216,
            "dry_run": True,
        }
    ]

    level = next(level for level in grid.levels if level.fill_count == 1)
    assert level.side == "sell"
    assert level.reserved_base == pytest.approx(1.0)
    assert level.reserved_quote == pytest.approx(0.0)
    assert level.price == pytest.approx(100.0)

    restored = GridBot(num_levels=2, amount_per_level_sol=1.0, executor=FakeExecutor(), state_path=state_path)
    persisted_level = next(level for level in restored.get_grid("SOL/USDC").levels if level.fill_count == 1)
    assert persisted_level.side == "sell"
    assert persisted_level.reserved_base == pytest.approx(1.0)


def test_check_grid_skips_unfunded_levels_when_balance_is_insufficient(tmp_path):
    bot = GridBot(num_levels=2, amount_per_level_sol=1.0, executor=FakeExecutor(), state_path=tmp_path / "grid_state.json")
    grid = bot.create_grid(
        "SOL/USDC",
        SOL_MINT,
        USDC_MINT,
        current_price=100.0,
        base_balance=0.0,
        quote_balance=10.0,
    )

    actions = bot.check_grid(grid, current_price=98.0, dry_run=True)

    assert len(actions) == 1
    assert actions[0]["status"] == "skipped"
    assert actions[0]["reason"] == "insufficient quote balance"
    assert bot.executor.calls == []


def test_run_once_recenters_when_price_moves_outside_grid_range(tmp_path):
    state_path = tmp_path / "grid_state.json"
    executor = FakeExecutor(
        responses=[
            {
                "status": "dry_run",
                "out_amount": 120_000_000,
            }
        ]
    )
    bot = GridBot(num_levels=1, amount_per_level_sol=1.0, executor=executor, state_path=state_path)
    grid = bot.create_grid(
        "SOL/USDC",
        SOL_MINT,
        USDC_MINT,
        current_price=100.0,
        base_balance=1.0,
        quote_balance=200.0,
    )

    actions = bot.run_once("SOL/USDC", current_price=120.0, dry_run=True)

    assert [action["action"] for action in actions] == ["SELL", "RECENTER"]
    assert grid.center_price == pytest.approx(120.0)
    assert grid.rebalance_count == 1
    assert grid.range_low < 120.0 < grid.range_high
    assert grid.unallocated_quote > 0
