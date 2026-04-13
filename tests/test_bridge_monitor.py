import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.bridge.monitor as bridge_monitor
from src.bridge.monitor import BridgeMonitor, BridgeTransfer


def test_mayan_completion_alerts_once_and_persists_state(tmp_path, monkeypatch):
    state_path = tmp_path / "bridge-state.json"
    alerts: list[tuple[str | None, str]] = []
    completions: list[tuple[str, str]] = []

    def fake_request_json(request_or_url, **kwargs):
        assert str(request_or_url).endswith("/swap/trx/0xsource")
        return {
            "sourceTxHash": "0xsource",
            "status": "SETTLED_ON_SOLANA",
            "completedAt": "2026-04-13T00:05:00Z",
            "initiatedAt": "2026-04-13T00:00:00Z",
            "sourceChain": "5",
            "destChain": "1",
            "destAddress": "So11111111111111111111111111111111111111112",
            "fromTokenSymbol": "WMATIC",
            "toTokenSymbol": "MSOL",
            "fromAmount": "10",
            "toAmount": "0.231",
            "destinationTxHash": "5xDestTx",
        }

    monkeypatch.setattr(bridge_monitor, "request_json", fake_request_json)
    monkeypatch.setattr(
        bridge_monitor.notifier,
        "warning",
        lambda message, title=None: alerts.append((title, message)),
    )

    monitor = BridgeMonitor(
        state_path=state_path,
        on_completion=lambda transfer, update: completions.append(
            (transfer.key, update.status)
        ),
    )
    monitor.track_transfer(
        BridgeTransfer.mayan(
            source_tx_hash="0xsource",
            eta_seconds=180,
        )
    )

    first_updates = monitor.poll_pending_transfers()
    second_updates = monitor.poll_pending_transfers()

    assert len(first_updates) == 1
    assert first_updates[0].status == "completed"
    assert first_updates[0].destination_tx_hash == "5xDestTx"
    assert second_updates == []
    assert len(alerts) == 1
    assert alerts[0][0] == "Bridge Transfer Completed"
    assert "Bridge: mayan" in alerts[0][1]
    assert "Destination Tx: 5xDestTx" in alerts[0][1]
    assert completions == [("mayan:0xsource", "completed")]

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["transfers"][0]["status"] == "completed"
    assert persisted["transfers"][0]["completion_alert_sent"] is True
    assert persisted["transfers"][0]["destination_tx_hash"] == "5xDestTx"


def test_debridge_resolves_order_id_and_saves_it_even_when_still_pending(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "bridge-state.json"

    def fake_request_json(request_or_url, **kwargs):
        url = str(request_or_url)
        if url.endswith("/Transaction/0xcreate/orderIds"):
            return {"orderIds": ["0xorder"]}
        if url.endswith("/Orders/0xorder"):
            return {
                "orderId": "0xorder",
                "status": "Created",
                "orderStruct": {
                    "receiverDst": "0xdestination",
                    "giveOffer": {"chainId": "1"},
                    "takeOffer": {"chainId": "7565164"},
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(bridge_monitor, "request_json", fake_request_json)
    monkeypatch.setattr(bridge_monitor.notifier, "warning", lambda *args, **kwargs: None)

    monitor = BridgeMonitor(state_path=state_path)
    transfer = monitor.track_transfer(
        BridgeTransfer.debridge(
            creation_tx_hash="0xcreate",
            approximate_fulfillment_delay=90,
            created_at="2026-04-13T00:00:00+00:00",
        )
    )

    updates = monitor.poll_pending_transfers()

    assert len(updates) == 1
    assert updates[0].status == "pending"
    assert updates[0].raw_status == "Created"
    assert updates[0].estimated_completion_time == "2026-04-13T00:01:30+00:00"
    assert transfer.order_id == "0xorder"
    assert transfer.destination_address == "0xdestination"

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["transfers"][0]["order_id"] == "0xorder"
    assert persisted["transfers"][0]["status"] == "pending"


def test_wormhole_operation_match_updates_destination_metadata(tmp_path, monkeypatch):
    state_path = tmp_path / "bridge-state.json"

    def fake_request_json(request_or_url, **kwargs):
        url = str(request_or_url)
        assert "operations?address=Emitter111&pageSize=25" in url
        return {
            "operations": [
                {
                    "id": "operation-1",
                    "sequence": "42",
                    "content": {
                        "standarizedProperties": {
                            "fromChain": 5,
                            "toChain": 1,
                            "toAddress": "Dest111",
                            "amount": "1000000",
                        }
                    },
                    "sourceChain": {
                        "timestamp": "2026-04-13T00:00:00Z",
                        "status": "confirmed",
                        "transaction": {"txHash": "0xsource"},
                    },
                    "targetChain": {
                        "timestamp": "2026-04-13T00:04:00Z",
                        "status": "confirmed",
                        "transaction": {"txHash": "0xdestination"},
                    },
                }
            ]
        }

    monkeypatch.setattr(bridge_monitor, "request_json", fake_request_json)
    monkeypatch.setattr(bridge_monitor.notifier, "warning", lambda *args, **kwargs: None)

    monitor = BridgeMonitor(state_path=state_path)
    transfer = monitor.track_transfer(
        BridgeTransfer.wormhole(
            emitter_address="Emitter111",
            source_tx_hash="0xsource",
        )
    )

    update = monitor.poll_transfer(transfer)

    assert update.status == "completed"
    assert update.destination_tx_hash == "0xdestination"
    assert update.completed_at == "2026-04-13T00:04:00Z"
    assert transfer.operation_id == "operation-1"
    assert transfer.sequence == "42"
    assert transfer.destination_address == "Dest111"
    assert transfer.destination_chain == "1"
