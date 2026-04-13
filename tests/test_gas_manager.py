import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.bridge.gas_manager as gas_manager
from src.bridge.gas_manager import GasChainConfig, GasManager


CHAIN_CONFIGS = {
    "solana": GasChainConfig(
        chain="solana",
        token_symbol="SOL",
        minimum_balance=0.10,
        warning_threshold=0.05,
        target_balance=0.20,
    ),
    "polygon": GasChainConfig(
        chain="polygon",
        token_symbol="POL",
        minimum_balance=2.0,
        warning_threshold=1.0,
        target_balance=4.0,
    ),
    "ethereum": GasChainConfig(
        chain="ethereum",
        token_symbol="ETH",
        minimum_balance=0.05,
        warning_threshold=0.02,
        target_balance=0.10,
    ),
}


def test_low_gas_warning_emits_once_until_balance_recovers(tmp_path, monkeypatch):
    state_path = tmp_path / "gas-manager.json"
    alerts: list[tuple[str | None, str]] = []

    monkeypatch.setattr(
        gas_manager.notifier,
        "warning",
        lambda message, title=None: alerts.append((title, message)),
    )

    manager = GasManager(
        state_path=state_path,
        chain_configs=CHAIN_CONFIGS,
        warning_cooldown_seconds=3600,
    )
    manager.update_balances(
        {
            "solana": {"balance": 0.20, "price_usd": 150.0},
            "polygon": {"balance": 0.04, "price_usd": 1.0},
            "ethereum": {"balance": 0.20, "price_usd": 3000.0},
        }
    )

    first_result = manager.run_cycle(auto_bridge=False)
    second_result = manager.run_cycle(auto_bridge=False)

    assert len(first_result.warnings) == 1
    assert "Polygon gas is low" in first_result.warnings[0]
    assert second_result.warnings == ()
    assert alerts == [("Low Gas Balance", first_result.warnings[0])]

    manager.update_balance("polygon", 2.5, price_usd=1.0)
    recovery_result = manager.run_cycle(auto_bridge=False)

    assert recovery_result.warnings == ()
    assert manager.states["polygon"].warning_active is False


def test_run_cycle_submits_small_gas_bridge_from_best_source(tmp_path, monkeypatch):
    state_path = tmp_path / "gas-manager.json"
    alerts: list[tuple[str | None, str]] = []
    submitted_actions: list[gas_manager.GasBridgeAction] = []

    monkeypatch.setattr(
        gas_manager.notifier,
        "warning",
        lambda message, title=None: alerts.append((title, message)),
    )

    def fake_bridge_executor(action):
        submitted_actions.append(action)
        return {"status": "submitted", "tracking_id": "bridge-123", "bridge": "mayan"}

    manager = GasManager(
        state_path=state_path,
        chain_configs=CHAIN_CONFIGS,
        bridge_executor=fake_bridge_executor,
        max_auto_bridge_usd=30.0,
    )
    manager.update_balances(
        {
            "solana": {"balance": 0.02, "price_usd": 150.0},
            "polygon": {"balance": 4.0, "price_usd": 1.0},
            "ethereum": {"balance": 0.20, "price_usd": 3000.0},
        }
    )

    result = manager.run_cycle()

    assert len(result.bridge_actions) == 1
    action = result.bridge_actions[0]
    assert action.source_chain == "ethereum"
    assert action.destination_chain == "solana"
    assert action.status == "submitted"
    assert action.provider_reference == "bridge-123"
    assert action.transfer_value_usd == pytest.approx(27.0)
    assert action.destination_amount == pytest.approx(0.18)
    assert action.source_amount_estimate == pytest.approx(0.009)
    assert submitted_actions[0].bridge_id == action.bridge_id

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["chains"]["solana"]["last_bridge_status"] == "submitted"
    assert persisted["chains"]["solana"]["last_bridge_source_chain"] == "ethereum"
    assert persisted["bridge_actions"][0]["provider_reference"] == "bridge-123"
    assert alerts == [("Low Gas Balance", result.warnings[0])]


def test_bridge_cooldown_survives_state_reload(tmp_path, monkeypatch):
    state_path = tmp_path / "gas-manager.json"

    monkeypatch.setattr(gas_manager.notifier, "warning", lambda *args, **kwargs: None)

    manager = GasManager(
        state_path=state_path,
        chain_configs=CHAIN_CONFIGS,
        bridge_cooldown_seconds=3600,
    )
    manager.update_balances(
        {
            "solana": {"balance": 0.01, "price_usd": 150.0},
            "polygon": {"balance": 4.0, "price_usd": 1.0},
            "ethereum": {"balance": 0.20, "price_usd": 3000.0},
        }
    )

    first_result = manager.run_cycle()
    second_result = manager.run_cycle()
    reloaded = GasManager(
        state_path=state_path,
        chain_configs=CHAIN_CONFIGS,
        bridge_cooldown_seconds=3600,
    )
    third_result = reloaded.run_cycle()

    assert len(first_result.bridge_actions) == 1
    assert first_result.bridge_actions[0].status == "planned"
    assert second_result.bridge_actions == ()
    assert third_result.bridge_actions == ()
