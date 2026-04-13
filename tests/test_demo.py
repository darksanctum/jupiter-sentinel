import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import demo


def test_run_demo_returns_mock_backed_report():
    report = demo.run_demo()

    assert report["mode"] == "demo"
    assert report["wallet"]["address"] == demo.DEMO_WALLET
    assert report["wallet"]["sol"] > 0
    assert report["scanner"]["pairs"]
    assert report["scanner"]["alerts"]
    assert report["arbitrage"]
    assert report["dex_intel"]["known_dexes"] >= 5
    assert report["trade_preview"]["status"] == "dry_run"
    assert report["mock_api_counts"]["quote"] > 0
    assert report["mock_api_counts"]["rpc.getBalance"] == 1
    assert report["mock_api_counts"]["labels"] == 1


def test_render_report_mentions_demo_mode_and_mock_usage():
    report = demo.run_demo()
    rendered = demo.render_report(report)

    assert "DEMO MODE" in rendered
    assert "Mock API Usage" in rendered
    assert "No wallet keys, API keys, or live network access were used." in rendered
