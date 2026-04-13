import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.state_manager import StateManager


def test_save_creates_backup_and_normalized_sections(tmp_path):
    path = tmp_path / "state.json"
    manager = StateManager(path=path, auto_save_interval=0.1)

    snapshot = {
        "dry_run": True,
        "cycle": 3,
        "open_positions": [{"position": {"pair": "JUP/USDC"}}],
        "closed_positions": [
            {
                "position": {"pair": "JUP/USDC"},
                "action": {"pnl_pct": 12.0},
                "realized_profit_sol": 0.25,
                "locked_profit_sol": 0.10,
            }
        ],
        "trade_history": [{"tx_signature": "tx-123"}],
    }

    manager.save(snapshot)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["bot_config"]["cycle"] == 3
    assert payload["positions"]["open"][0]["position"]["pair"] == "JUP/USDC"
    assert payload["profit_tracking"]["realized_profit_sol"] == 0.25
    assert payload["profit_tracking"]["locked_profit_sol"] == 0.10
    assert path.with_suffix(".json.bak").exists()


def test_load_recovers_from_backup_when_primary_file_is_corrupt(tmp_path):
    path = tmp_path / "state.json"
    manager = StateManager(path=path, auto_save_interval=0.1)
    manager.save(
        {
            "dry_run": True,
            "open_positions": [{"position": {"pair": "JUP/USDC"}}],
            "trade_history": [{"tx_signature": "tx-123"}],
        }
    )

    path.write_text("{invalid-json", encoding="utf-8")

    recovered = StateManager(path=path, auto_save_interval=0.1).load()
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert recovered["positions"]["open"][0]["position"]["pair"] == "JUP/USDC"
    assert persisted["positions"]["open"][0]["position"]["pair"] == "JUP/USDC"
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


def test_load_archives_corrupt_state_and_rebuilds_default_when_no_backup_exists(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{invalid-json", encoding="utf-8")

    rebuilt = StateManager(path=path, auto_save_interval=0.1).load()
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert rebuilt["positions"]["open"] == []
    assert rebuilt["trade_history"] == []
    assert persisted["positions"]["closed"] == []
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


def test_auto_save_persists_dirty_state(tmp_path):
    path = tmp_path / "state.json"
    manager = StateManager(path=path, auto_save_interval=0.05)

    try:
        manager.start_auto_save()
        manager.update(
            open_positions=[{"position": {"pair": "JUP/USDC"}}],
            bot_config={"cycle": 4},
        )

        deadline = time.time() + 1.0
        while time.time() < deadline and not path.exists():
            time.sleep(0.02)
    finally:
        manager.stop_auto_save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["positions"]["open"][0]["position"]["pair"] == "JUP/USDC"
    assert payload["bot_config"]["cycle"] == 4


def test_concurrent_saves_leave_valid_json(tmp_path):
    path = tmp_path / "state.json"
    manager = StateManager(path=path, auto_save_interval=0.1)

    def worker(index: int) -> None:
        for cycle in range(10):
            manager.save(
                {
                    "dry_run": True,
                    "cycle": cycle,
                    "open_positions": [{"position": {"pair": f"PAIR-{index}"}}],
                    "trade_history": [{"worker": index, "cycle": cycle}],
                }
            )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "positions" in payload
    assert payload["bot_config"]["dry_run"] is True
    assert len(payload["trade_history"]) == 1
