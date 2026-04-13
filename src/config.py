"""
Jupiter Sentinel - Configuration
"""

import logging
from typing import Any
import os
import json
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .validation import validate_solana_address

if TYPE_CHECKING:
    from solders.keypair import Keypair

# Wallet
RPC_URL = "https://api.mainnet-beta.solana.com"

# Jupiter API
JUPITER_BASE = "https://api.jup.ag"
JUPITER_SWAP_V1 = f"{JUPITER_BASE}/swap/v1"
JUPITER_SWAP_V2 = f"{JUPITER_BASE}/swap/v2"

# Tokens
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF_MINT = "EKpQGSJtjMFqWwMJL7pYqznSHQB3TV8sR4Y5DxmAdJbx"

# Popular pairs to scan
SCAN_PAIRS = [
    (SOL_MINT, USDC_MINT, "SOL/USDC"),
    (JUP_MINT, USDC_MINT, "JUP/USDC"),
    (JUP_MINT, SOL_MINT, "JUP/SOL"),
    (BONK_MINT, USDC_MINT, "BONK/USDC"),
    (WIF_MINT, USDC_MINT, "WIF/USDC"),
]

# Risk parameters
MAX_POSITION_USD = 5.0  # Max $5 per position (we're working with small capital)
STOP_LOSS_BPS = 500  # 5% stop loss
TAKE_PROFIT_BPS = 1500  # 15% take profit
VOLATILITY_THRESHOLD = 0.03  # 3% price move = volatile
SCAN_INTERVAL_SECS = 30  # Scan every 30 seconds
PRICE_HISTORY_LEN = 60  # Keep 60 data points (30 min at 30s intervals)

# Paths
PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


def _build_headers() -> dict[str, str]:
    """Function docstring."""
    headers = {
        "User-Agent": "JupiterSentinel/1.0",
        "Content-Type": "application/json",
    }

    api_key = os.environ.get("JUP_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


HEADERS = _build_headers()


def _get_private_key_path() -> Path:
    """Function docstring."""
    raw_path = os.environ.get("SOLANA_PRIVATE_KEY_PATH", "").strip()
    if not raw_path:
        raise RuntimeError(
            "SOLANA_PRIVATE_KEY_PATH is required for signing transactions. "
            "Use SOLANA_PUBLIC_KEY for read-only wallet access."
        )

    path = Path(raw_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Private key file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Private key path is not a file: {path}")

    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                "Private key file permissions are too open. Restrict access with chmod 600."
            )

    return path


def load_keypair() -> "Keypair":
    """Load Solana keypair from file."""
    from solders.keypair import Keypair

    with _get_private_key_path().open(encoding="utf-8") as handle:
        key_bytes = json.load(handle)

    if not isinstance(key_bytes, list) or len(key_bytes) != 64:
        raise ValueError("Private key file must contain a 64-byte JSON array")
    if any(
        not isinstance(value, int) or value < 0 or value > 255 for value in key_bytes
    ):
        raise ValueError(
            "Private key file must contain only integer byte values between 0 and 255"
        )

    return Keypair.from_bytes(bytes(key_bytes))


def get_pubkey() -> str:
    """Get wallet public key from a public env var or the configured keypair."""
    configured_pubkey = os.environ.get("SOLANA_PUBLIC_KEY", "").strip()
    if configured_pubkey:
        return validate_solana_address(configured_pubkey, "SOLANA_PUBLIC_KEY")

    kp = load_keypair()
    return validate_solana_address(str(kp.pubkey()), "wallet_pubkey")
