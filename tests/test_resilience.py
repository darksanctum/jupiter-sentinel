import errno
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.resilience as resilience


def test_write_json_state_warns_and_switches_to_memory_only_mode(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    payload = {"open_positions": [{"pair": "JUP/USDC"}]}

    def fail_atomic_write(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(resilience, "atomic_write_text", fail_atomic_write)

    with pytest.warns(RuntimeWarning, match="memory-only mode"):
        written = resilience.write_json_state(path, payload)

    assert written is False
    assert resilience.in_memory_only_mode(path) is True
    assert resilience.read_json_file(path) == payload


def test_reconcile_transaction_state_updates_recent_chain_statuses(monkeypatch):
    now = datetime.utcnow().isoformat()
    state = {
        "trade_history": [
            {"tx_signature": "sig-finalized", "status": "success", "timestamp": now},
            {"tx_signature": "sig-failed", "status": "pending", "timestamp": now},
        ],
        "open_positions": [
            {
                "position": {"pair": "JUP/USDC"},
                "meta": {
                    "entry_result": {
                        "tx_signature": "sig-pending",
                        "status": "success",
                        "timestamp": now,
                    }
                },
            }
        ],
        "closed_positions": [
            {
                "exit_result": {
                    "tx_signature": "sig-dry-run",
                    "status": "dry_run",
                    "timestamp": now,
                }
            }
        ],
    }

    def fake_request_json(request, *, timeout, **kwargs):
        body = json.loads(request.data.decode())
        assert body["method"] == "getSignatureStatuses"
        assert body["params"][0] == ["sig-finalized", "sig-failed", "sig-pending"]
        return {
            "result": {
                "value": [
                    {"confirmationStatus": "finalized", "slot": 10, "err": None},
                    {"confirmationStatus": "processed", "slot": 11, "err": {"InstructionError": [0, "Custom"]}},
                    {"confirmationStatus": "processed", "slot": 12, "err": None},
                ]
            }
        }

    monkeypatch.setattr(resilience, "request_json", fake_request_json)

    result = resilience.reconcile_transaction_state(state)
    updated = result["state"]

    assert result["changed"] is True
    assert updated["trade_history"][0]["status"] == "success"
    assert updated["trade_history"][0]["confirmation_status"] == "finalized"
    assert updated["trade_history"][0]["slot"] == 10

    assert updated["trade_history"][1]["status"] == "failed"
    assert updated["trade_history"][1]["confirmation_status"] == "processed"
    assert updated["trade_history"][1]["error"] == {"InstructionError": [0, "Custom"]}

    assert updated["open_positions"][0]["meta"]["entry_result"]["status"] == "pending"
    assert updated["open_positions"][0]["meta"]["entry_result"]["confirmation_status"] == "processed"
    assert updated["closed_positions"][0]["exit_result"]["status"] == "dry_run"

    assert [tx.signature for tx in result["transactions"]] == ["sig-finalized", "sig-failed", "sig-pending"]


def test_has_reconcilable_transactions_ignores_dry_run_entries():
    assert resilience.has_reconcilable_transactions({"trade_history": [{"tx_signature": "dry", "status": "dry_run"}]}) is False
    assert resilience.has_reconcilable_transactions({"trade_history": [{"tx_signature": "live", "status": "success"}]}) is True
