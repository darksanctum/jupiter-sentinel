"""
Jupiter Sentinel - Configuration
"""
import os
import json
from pathlib import Path

# Wallet
SOLANA_KEY_PATH = os.environ.get(
    "SOLANA_PRIVATE_KEY_PATH",
    os.path.expanduser("~/.clawd/secrets/SOLANA_SHADOW_001_KEY")
)
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
MAX_POSITION_USD = 5.0         # Max $5 per position (we're working with small capital)
STOP_LOSS_BPS = 500            # 5% stop loss
TAKE_PROFIT_BPS = 1500         # 15% take profit
VOLATILITY_THRESHOLD = 0.03    # 3% price move = volatile
SCAN_INTERVAL_SECS = 30        # Scan every 30 seconds
PRICE_HISTORY_LEN = 60         # Keep 60 data points (30 min at 30s intervals)

# Paths
PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# Headers
HEADERS = {
    "User-Agent": "JupiterSentinel/1.0",
    "Content-Type": "application/json",
}


def load_keypair():
    """Load Solana keypair from file."""
    from solders.keypair import Keypair
    
    with open(SOLANA_KEY_PATH) as f:
        key_bytes = json.loads(f.read())
    
    return Keypair.from_bytes(bytes(key_bytes))


def get_pubkey():
    """Get wallet public key without loading full keypair."""
    kp = load_keypair()
    return str(kp.pubkey())
