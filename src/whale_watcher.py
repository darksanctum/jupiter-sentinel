import time
import logging
from typing import Dict
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from solders.signature import Signature
from solana.rpc.core import RPCException

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Known exchange hot wallets
EXCHANGES = {
    # Binance Hot Wallet 1
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdK1zV3s2pQG": "Binance",
    # Coinbase Hot Wallet
    "2AQdpHJ2JpcEgPiATv4VK29HMB3HXXAtoZgA9w9n89B3": "Coinbase",
    # Kraken Hot Wallet
    "5tzFkiKscXHK5ZXCGbXZzY3FZXCGbXZzY3FZXCGb": "Kraken",
}

# 1 SOL = 1,000,000,000 lamports
LAMPORTS_PER_SOL = 1_000_000_000
WHALE_THRESHOLD_SOL = 100.0

class WhaleWatcher:
    def __init__(self, rpc_url: str = "https://api.mainnet-beta.solana.com"):
        self.client = Client(rpc_url)
        self.exchanges = {Pubkey.from_string(k): v for k, v in EXCHANGES.items()}
        self.last_signatures: Dict[Pubkey, Signature] = {pk: None for pk in self.exchanges.keys()}

    def start(self, poll_interval: int = 10):
        logger.info("Starting Whale Watcher...")
        logger.info(f"Whale Threshold: {WHALE_THRESHOLD_SOL} SOL")
        
        while True:
            try:
                self.check_exchanges()
            except Exception as e:
                logger.error(f"Error checking exchanges: {e}")
            
            time.sleep(poll_interval)

    def check_exchanges(self):
        for exchange_pubkey, exchange_name in self.exchanges.items():
            self._check_exchange(exchange_pubkey, exchange_name)

    def _check_exchange(self, exchange_pubkey: Pubkey, exchange_name: str):
        # Fetch recent signatures for the exchange
        last_sig = self.last_signatures[exchange_pubkey]
        kwargs = {"limit": 10}
        if last_sig is not None:
            kwargs["until"] = last_sig
            
        try:
            response = self.client.get_signatures_for_address(exchange_pubkey, **kwargs)
            if not response.value:
                return
            
            signatures = response.value

            # Update last signature
            self.last_signatures[exchange_pubkey] = signatures[0].signature

            # If it's the first run, don't alert on all past transactions, just set the checkpoint
            if last_sig is None:
                return

            for sig_info in signatures:
                if sig_info.err is not None:
                    continue  # skip failed txs
                self._process_transaction(sig_info.signature, exchange_pubkey, exchange_name)
                
        except RPCException as e:
            logger.error(f"RPC Error fetching signatures for {exchange_name}: {e}")
        except Exception as e:
            logger.error(f"Error checking {exchange_name}: {e}")

    def _process_transaction(self, signature: Signature, exchange_pubkey: Pubkey, exchange_name: str):
        try:
            # fetch transaction info
            tx_resp = self.client.get_transaction(signature, max_supported_transaction_version=0)
            if not tx_resp or not tx_resp.value:
                return

            meta = tx_resp.value.transaction.meta
            if meta.err is not None:
                return # Transaction failed

            tx = tx_resp.value.transaction.transaction
            
            # Find the index of the exchange account
            account_keys = tx.message.account_keys
            exchange_index = -1
            for i, key in enumerate(account_keys):
                if key == exchange_pubkey:
                    exchange_index = i
                    break
                    
            if exchange_index == -1:
                return
                
            pre_balance = meta.pre_balances[exchange_index]
            post_balance = meta.post_balances[exchange_index]
            
            net_change_lamports = post_balance - pre_balance
            net_change_sol = net_change_lamports / LAMPORTS_PER_SOL
            
            if abs(net_change_sol) >= WHALE_THRESHOLD_SOL:
                if net_change_sol > 0:
                    # Exchange balance increased -> someone sent TO exchange -> SELL SIGNAL
                    logger.warning(f"🚨 WHALE SELL SIGNAL: {abs(net_change_sol):.2f} SOL moved TO {exchange_name}")
                    logger.info(f"Tx: https://solscan.io/tx/{signature}")
                else:
                    # Exchange balance decreased -> someone withdrew FROM exchange -> BUY SIGNAL
                    logger.info(f"🐳 WHALE BUY SIGNAL: {abs(net_change_sol):.2f} SOL moved FROM {exchange_name} to wallet")
                    logger.info(f"Tx: https://solscan.io/tx/{signature}")

        except Exception as e:
            logger.error(f"Error processing tx {signature}: {e}")

if __name__ == "__main__":
    watcher = WhaleWatcher()
    watcher.start()
