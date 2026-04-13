"""
Security helpers for redacting secrets and avoiding wallet exposure in logs.
"""

from __future__ import annotations
import logging

import os
import re
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_ENV_VARS = (
    "JUP_API_KEY",
    "SOLANA_PRIVATE_KEY",
    "SOLANA_PRIVATE_KEY_JSON",
    "SOLANA_PRIVATE_KEY_PATH",
    "TELEGRAM_BOT_TOKEN",
)
_BOT_TOKEN_IN_URL = re.compile(
    r"(https://api\.telegram\.org/bot)([^/\s]+)", re.IGNORECASE
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|private[_ -]?key|secret|token|mnemonic|seed phrase)\b\s*[:=]\s*([^\s,;]+)"
)
_LONG_BASE58_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{80,128}(?![A-Za-z0-9])"
)


def sanitize_sensitive_text(value: Any) -> str:
    """
    Best-effort redaction for exception messages and log output.

    The goal is to keep normal operational errors readable while scrubbing
    likely secret-bearing values if they accidentally show up in an exception.
    """
    text = str(value)

    for env_name in _SECRET_ENV_VARS:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            text = text.replace(env_value, REDACTED)

    text = _BOT_TOKEN_IN_URL.sub(r"\1" + REDACTED, text)
    text = _KEY_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    text = _LONG_BASE58_PATTERN.sub(REDACTED, text)
    return text


def display_wallet_status(address: Any) -> str:
    """Function docstring."""
    text = str(address or "").strip()
    if not text or text == "unconfigured":
        return "unconfigured"
    return "configured (redacted)"
