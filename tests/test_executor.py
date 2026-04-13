import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.executor as executor
from src.executor import TradeExecutor

VALID_INPUT_MINT = executor.SOL_MINT
VALID_OUTPUT_MINT = executor.USDC_MINT
VALID_ALT_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


class FakeKeypair:
    def pubkey(self):
        return "fake-pubkey"


def make_executor(monkeypatch):
    monkeypatch.setattr(executor, "load_keypair", lambda: FakeKeypair())
    return TradeExecutor()


def install_urlopen(monkeypatch, *responses):
    queue = list(responses)
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append((request, timeout))
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return FakeResponse(response)

    monkeypatch.setattr(executor.urllib.request, "urlopen", fake_urlopen)
    return calls


def install_fake_transaction_stack(monkeypatch):
    seen = {}

    class FakeVersionedTransaction:
        def __init__(self, message, signers):
            self.message = message
            self.signers = signers

        @classmethod
        def from_bytes(cls, tx_bytes):
            seen["decoded_tx_bytes"] = tx_bytes
            return types.SimpleNamespace(message="fake-message")

        def __bytes__(self):
            return b"signed-tx-bytes"

    fake_solders = types.ModuleType("solders")
    fake_transaction = types.ModuleType("solders.transaction")
    fake_transaction.VersionedTransaction = FakeVersionedTransaction
    fake_solders.transaction = fake_transaction

    monkeypatch.setitem(sys.modules, "solders", fake_solders)
    monkeypatch.setitem(sys.modules, "solders.transaction", fake_transaction)

    monkeypatch.setattr(executor.base58, "b58decode", lambda value: b"decoded-swap-tx")

    def fake_b58encode(value):
        seen["encoded_input"] = value
        return b"encoded-signed-tx"

    monkeypatch.setattr(executor.base58, "b58encode", fake_b58encode)
    return seen


def test_get_quote_builds_request_and_parses_response(monkeypatch):
    calls = install_urlopen(monkeypatch, {"outAmount": "12345", "routePlan": []})
    trade_executor = make_executor(monkeypatch)

    quote = trade_executor.get_quote(VALID_INPUT_MINT, VALID_OUTPUT_MINT, 42, slippage_bps=75)

    assert quote == {"outAmount": "12345", "routePlan": []}

    request, timeout = calls[0]
    assert timeout == 15
    assert request.full_url == (
        f"{executor.JUPITER_SWAP_V1}/quote?"
        f"inputMint={VALID_INPUT_MINT}&"
        f"outputMint={VALID_OUTPUT_MINT}&"
        f"amount=42&"
        f"slippageBps=75&"
        f"onlyDirectRoutes=false&"
        f"asLegacyTransaction=false"
    )
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["user-agent"] == executor.HEADERS["User-Agent"]
    assert headers["content-type"] == executor.HEADERS["Content-Type"]
    if "x-api-key" in executor.HEADERS:
        assert headers["x-api-key"] == executor.HEADERS["x-api-key"]


def test_execute_swap_returns_failed_when_quote_is_missing(monkeypatch):
    trade_executor = make_executor(monkeypatch)
    monkeypatch.setattr(trade_executor, "get_quote", lambda *args, **kwargs: None)

    result = trade_executor.execute_swap("mint-in", "mint-out", 1_000, dry_run=False)

    assert result["status"] == "failed"
    assert result["error"] == "No quote returned"
    assert trade_executor.trade_history == []


def test_execute_swap_dry_run_calculates_sol_output_usd_value(monkeypatch):
    trade_executor = make_executor(monkeypatch)
    calls = []

    def fake_get_quote(input_mint, output_mint, amount, slippage_bps=300):
        calls.append((input_mint, output_mint, amount, slippage_bps))
        if output_mint == executor.SOL_MINT:
            return {
                "outAmount": "2000000000",
                "priceImpactPct": "0.15",
                "routePlan": [{"swapInfo": "main-route"}],
            }
        return {"outAmount": "150000"}

    monkeypatch.setattr(trade_executor, "get_quote", fake_get_quote)

    result = trade_executor.execute_swap(VALID_ALT_MINT, executor.SOL_MINT, 1_000_000, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["out_amount"] == 2_000_000_000
    assert result["price_impact"] == pytest.approx(0.15)
    assert result["route_plan"] == [{"swapInfo": "main-route"}]
    assert result["out_usd"] == pytest.approx(300.0)
    assert calls == [
        (VALID_ALT_MINT, executor.SOL_MINT, 1_000_000, 300),
        (executor.SOL_MINT, executor.USDC_MINT, 1_000_000, 50),
    ]
    assert trade_executor.trade_history == []


def test_execute_swap_success_signs_and_broadcasts_transaction(monkeypatch):
    calls = install_urlopen(
        monkeypatch,
        {"swapTransaction": "base58-swap-transaction"},
        {"result": "tx-signature-123"},
    )
    seen = install_fake_transaction_stack(monkeypatch)
    trade_executor = make_executor(monkeypatch)
    monkeypatch.setattr(
        trade_executor,
        "get_quote",
        lambda *args, **kwargs: {
            "outAmount": "2500000",
            "priceImpactPct": "0.25",
            "routePlan": [{"percent": 100}],
        },
    )

    result = trade_executor.execute_swap(
        executor.SOL_MINT,
        executor.USDC_MINT,
        1_000_000_000,
        dry_run=False,
    )

    assert result["status"] == "success"
    assert result["out_amount"] == 2_500_000
    assert result["out_usd"] == pytest.approx(2.5)
    assert result["tx_signature"] == "tx-signature-123"
    assert result["solscan"] == "https://solscan.io/tx/tx-signature-123"
    assert trade_executor.trade_history == [result]
    assert seen["decoded_tx_bytes"] == b"decoded-swap-tx"
    assert seen["encoded_input"] == b"signed-tx-bytes"

    swap_request, swap_timeout = calls[0]
    assert swap_timeout == 15
    assert swap_request.full_url == f"{executor.JUPITER_SWAP_V1}/swap"
    assert json.loads(swap_request.data) == {
        "quoteResponse": {
            "outAmount": "2500000",
            "priceImpactPct": "0.25",
            "routePlan": [{"percent": 100}],
        },
        "userPublicKey": "fake-pubkey",
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }

    rpc_request, rpc_timeout = calls[1]
    assert rpc_timeout == 30
    assert rpc_request.full_url == executor.RPC_URL
    assert json.loads(rpc_request.data) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            "encoded-signed-tx",
            {"encoding": "base58", "skipPreflight": True},
        ],
    }
    rpc_headers = {key.lower(): value for key, value in rpc_request.header_items()}
    assert rpc_headers == {
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0",
    }


def test_execute_swap_records_failed_rpc_response(monkeypatch):
    install_urlopen(
        monkeypatch,
        {"swapTransaction": "base58-swap-transaction"},
        {"error": {"message": "rpc rejected"}},
    )
    install_fake_transaction_stack(monkeypatch)
    trade_executor = make_executor(monkeypatch)
    monkeypatch.setattr(
        trade_executor,
        "get_quote",
        lambda *args, **kwargs: {"outAmount": "2500000", "routePlan": []},
    )

    result = trade_executor.execute_swap("mint-in", executor.USDC_MINT, 1_000, dry_run=False)

    assert result["status"] == "failed"
    assert result["error"] == "rpc rejected"
    assert trade_executor.trade_history == [result]


def test_execute_swap_catches_runtime_exceptions(monkeypatch):
    trade_executor = make_executor(monkeypatch)
    monkeypatch.setattr(
        trade_executor,
        "get_quote",
        lambda *args, **kwargs: {"outAmount": "123", "routePlan": []},
    )

    def fail_urlopen(request, timeout=0):
        raise RuntimeError("swap endpoint unavailable")

    monkeypatch.setattr(executor.urllib.request, "urlopen", fail_urlopen)

    result = trade_executor.execute_swap("mint-in", executor.USDC_MINT, 1_000, dry_run=False)

    assert result["status"] == "error"
    assert result["error"] == "swap endpoint unavailable"
    assert trade_executor.trade_history == [result]


def test_get_balance_returns_sol_and_usd_values(monkeypatch):
    calls = install_urlopen(monkeypatch, {"result": {"value": 2_500_000_000}})
    trade_executor = make_executor(monkeypatch)
    monkeypatch.setattr(
        trade_executor,
        "get_quote",
        lambda *args, **kwargs: {"outAmount": "160000"},
    )

    balance = trade_executor.get_balance()

    assert balance == {
        "sol": pytest.approx(2.5),
        "usd_value": pytest.approx(400.0),
        "sol_price": pytest.approx(160.0),
        "address": "fake-pubkey",
    }

    request, timeout = calls[0]
    assert timeout == 10
    assert request.full_url == executor.RPC_URL
    assert json.loads(request.data) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": ["fake-pubkey"],
    }


def test_get_balance_returns_unconfigured_wallet_when_keys_are_missing(monkeypatch):
    monkeypatch.setattr(executor, "get_pubkey", lambda: (_ for _ in ()).throw(RuntimeError("missing wallet")))
    trade_executor = TradeExecutor()
    monkeypatch.setattr(
        trade_executor,
        "get_quote",
        lambda *args, **kwargs: {"outAmount": "160000"},
    )

    balance = trade_executor.get_balance()

    assert balance == {
        "sol": 0.0,
        "usd_value": 0.0,
        "sol_price": pytest.approx(160.0),
        "address": "unconfigured",
    }
