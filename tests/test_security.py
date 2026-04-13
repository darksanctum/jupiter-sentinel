import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.security import REDACTED, display_wallet_status, sanitize_sensitive_text


def test_sanitize_sensitive_text_redacts_bot_tokens(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:secret-token")

    rendered = sanitize_sensitive_text("POST https://api.telegram.org/bot123456:secret-token/sendMessage failed")

    assert "secret-token" not in rendered
    assert REDACTED in rendered


def test_display_wallet_status_never_returns_full_wallet():
    assert display_wallet_status("unconfigured") == "unconfigured"
    assert display_wallet_status("DemoWallet111111111111111111111111111111111") == "configured (redacted)"
