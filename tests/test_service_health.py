import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import src.risk as risk
from src.autotrader import AutoTrader
from src.correlation_tracker import CorrelationTracker
from src.service_health import BotHealthServer


class EmptyScanner:
    def __init__(self) -> None:
        self.feeds = []

    def scan_once(self):
        return []

    def stop(self) -> None:
        return None


class DummyExecutor:
    def get_balance(self):
        return {
            "sol": 10.0,
            "usd_value": 200.0,
            "sol_price": 20.0,
            "address": "health-test-wallet",
        }


def build_trader(tmp_path):
    executor = DummyExecutor()
    return AutoTrader(
        dry_run=True,
        state_path=tmp_path / "state.json",
        scanner=EmptyScanner(),
        executor=executor,
        risk_manager=risk.RiskManager(executor),
        correlation_tracker=CorrelationTracker(path=tmp_path / "correlations.json"),
        scan_interval_secs=30,
        sleep_fn=lambda _: None,
    )


def test_autotrader_health_snapshot_reports_ok_and_stale_states(tmp_path) -> None:
    trader = build_trader(tmp_path)
    now = time.time()

    trader.running = True
    trader.started_at = now - 15
    trader.last_cycle_started_at = now - 5
    trader.last_successful_cycle_at = now - 1

    status_code, payload = trader.get_health_snapshot(stale_after_secs=30)
    assert status_code == 200
    assert payload["status"] == "ok"
    assert payload["healthy"] is True
    assert payload["mode"] == "dry-run"
    assert payload["state_file"].endswith("state.json")

    trader.last_successful_cycle_at = now - 120
    status_code, payload = trader.get_health_snapshot(stale_after_secs=30)
    assert status_code == 503
    assert payload["status"] == "stale"
    assert payload["healthy"] is False


class StaticProvider:
    def get_health_snapshot(self, stale_after_secs: float = 180.0):
        return 200, {
            "service": "jupiter-sentinel",
            "status": "ok",
            "healthy": True,
            "stale_after_secs": stale_after_secs,
        }


def test_health_server_serves_http_health_payload() -> None:
    try:
        server = BotHealthServer(
            StaticProvider(),
            host="127.0.0.1",
            port=0,
            stale_after_secs=45.0,
        )
    except PermissionError:
        pytest.skip("Local socket binding is not permitted in this sandbox")
    server.start()

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}{server.health_path}",
            timeout=5,
        ) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["status"] == "ok"
    assert payload["stale_after_secs"] == 45.0
