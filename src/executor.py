"""
Jupiter Sentinel - Trade Executor
Executes swaps via Jupiter Swap V1 API with risk management.
"""
import json
import time
import urllib.request
import base58
from typing import Any, Optional
from datetime import datetime

from .config import (
    JUPITER_SWAP_V1, HEADERS, RPC_URL,
    SOL_MINT, USDC_MINT, load_keypair
)


class TradeExecutor:
    """
    Executes trades via Jupiter's Swap API.
    Handles quote fetching, transaction signing, and broadcasting.
    """
    
    def __init__(self) -> None:
        self.keypair = load_keypair()
        self.pubkey = str(self.keypair.pubkey())
        self.trade_history: list[dict[str, Any]] = []
    
    def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 300,
    ) -> Optional[dict[str, Any]]:
        """Get a swap quote from Jupiter."""
        url = (
            f"{JUPITER_SWAP_V1}/quote?"
            f"inputMint={input_mint}&"
            f"outputMint={output_mint}&"
            f"amount={amount}&"
            f"slippageBps={slippage_bps}&"
            f"onlyDirectRoutes=false&"
            f"asLegacyTransaction=false"
        )
        
        req = urllib.request.Request(url, headers=HEADERS)
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return resp
    
    def execute_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 300,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Execute a swap via Jupiter.
        
        Args:
            input_mint: Input token mint address
            output_mint: Output token mint address
            amount: Amount in smallest unit
            slippage_bps: Slippage tolerance in basis points
            dry_run: If True, only get quote without executing
        
        Returns:
            dict with quote details and tx signature (if executed)
        """
        result: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "input_mint": input_mint,
            "output_mint": output_mint,
            "amount": amount,
            "status": "pending",
        }
        
        try:
            # 1. Get quote
            quote = self.get_quote(input_mint, output_mint, amount, slippage_bps)
            if not quote:
                result["status"] = "failed"
                result["error"] = "No quote returned"
                return result
            
            out_amount = int(quote["outAmount"])
            price_impact = float(quote.get("priceImpactPct", 0))
            
            result["out_amount"] = out_amount
            result["price_impact"] = price_impact
            result["route_plan"] = quote.get("routePlan", [])
            
            # Get USD value
            if output_mint == USDC_MINT:
                result["out_usd"] = out_amount / 1e6
            elif output_mint == SOL_MINT:
                # Get SOL price
                sol_quote = self.get_quote(SOL_MINT, USDC_MINT, 1_000_000, 50)
                if sol_quote:
                    sol_price = int(sol_quote["outAmount"]) / 1e6 / 0.001
                    result["out_usd"] = (out_amount / 1e9) * sol_price
            
            if dry_run:
                result["status"] = "dry_run"
                print(f"[DRY RUN] {amount/1e9:.6f} -> {out_amount} (impact: {price_impact:.2f}%)")
                return result
            
            # 2. Get swap transaction
            swap_url = f"{JUPITER_SWAP_V1}/swap"
            swap_data = json.dumps({
                "quoteResponse": quote,
                "userPublicKey": self.pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }).encode()
            
            req = urllib.request.Request(swap_url, data=swap_data, headers=HEADERS)
            swap_resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            
            # 3. Sign transaction
            tx_bytes = base58.b58decode(swap_resp["swapTransaction"])
            from solders.transaction import VersionedTransaction
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            
            # 4. Broadcast
            encoded = base58.b58encode(bytes(signed_tx)).decode()
            rpc_body = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    encoded,
                    {"encoding": "base58", "skipPreflight": True},
                ],
            }).encode()
            
            req = urllib.request.Request(
                RPC_URL,
                data=rpc_body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            rpc_resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            
            if "result" in rpc_resp:
                result["status"] = "success"
                result["tx_signature"] = rpc_resp["result"]
                result["solscan"] = f"https://solscan.io/tx/{rpc_resp['result']}"
                print(f"[EXECUTED] TX: {rpc_resp['result'][:20]}...")
            else:
                result["status"] = "failed"
                result["error"] = rpc_resp.get("error", {}).get("message", "Unknown error")
                print(f"[FAILED] {result['error']}")
            
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            print(f"[ERROR] {e}")
        
        self.trade_history.append(result)
        return result
    
    def get_balance(self) -> dict[str, Any]:
        """Get wallet SOL balance."""
        rpc_body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [self.pubkey],
        }).encode()
        
        req = urllib.request.Request(
            RPC_URL,
            data=rpc_body,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        
        sol_balance = resp.get("result", {}).get("value", 0) / 1e9
        
        # Get USD value
        quote = self.get_quote(SOL_MINT, USDC_MINT, 1_000_000, 50)
        sol_price = int(quote["outAmount"]) / 1e6 / 0.001 if quote else 0
        
        return {
            "sol": sol_balance,
            "usd_value": sol_balance * sol_price,
            "sol_price": sol_price,
            "address": self.pubkey,
        }


if __name__ == "__main__":
    executor = TradeExecutor()
    balance = executor.get_balance()
    print(f"Wallet: {balance['address']}")
    print(f"Balance: {balance['sol']:.6f} SOL (${balance['usd_value']:.2f})")
