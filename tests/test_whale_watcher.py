import pytest
import logging
from unittest.mock import MagicMock
from solders.pubkey import Pubkey
from solders.signature import Signature
from src.whale_watcher import WhaleWatcher


def build_watcher():
    exchange_pk = Pubkey.default()
    watcher = WhaleWatcher(exchange_wallets={str(exchange_pk): "Binance"})
    return watcher, exchange_pk


def test_whale_watcher_init():
    watcher = WhaleWatcher()
    assert watcher.client is not None
    assert watcher.exchanges == {}


def test_whale_watcher_init_with_explicit_exchange_wallets():
    watcher, exchange_pk = build_watcher()
    assert watcher.exchanges == {exchange_pk: "Binance"}

def test_process_transaction_sell_signal(caplog):
    watcher, exchange_pk = build_watcher()
    watcher.client = MagicMock()
    
    mock_tx_resp = MagicMock()
    mock_tx_resp.value = MagicMock()
    mock_tx_resp.value.transaction.meta.err = None
    mock_tx_resp.value.transaction.meta.pre_balances = [10_000_000_000] # 10 SOL
    mock_tx_resp.value.transaction.meta.post_balances = [160_000_000_000] # 160 SOL (diff +150)
    
    mock_tx_resp.value.transaction.transaction.message.account_keys = [exchange_pk]
    
    watcher.client.get_transaction.return_value = mock_tx_resp
    
    sig = Signature.default()
    
    with caplog.at_level(logging.INFO):
        watcher._process_transaction(sig, exchange_pk, "Binance")
        
    assert "WHALE SELL SIGNAL: 150.00 SOL moved TO Binance" in caplog.text

def test_process_transaction_buy_signal(caplog):
    watcher, exchange_pk = build_watcher()
    watcher.client = MagicMock()
    
    mock_tx_resp = MagicMock()
    mock_tx_resp.value = MagicMock()
    mock_tx_resp.value.transaction.meta.err = None
    mock_tx_resp.value.transaction.meta.pre_balances = [200_000_000_000] # 200 SOL
    mock_tx_resp.value.transaction.meta.post_balances = [50_000_000_000] # 50 SOL (diff -150)
    
    mock_tx_resp.value.transaction.transaction.message.account_keys = [exchange_pk]
    
    watcher.client.get_transaction.return_value = mock_tx_resp
    
    sig = Signature.default()
    
    with caplog.at_level(logging.INFO):
        watcher._process_transaction(sig, exchange_pk, "Binance")
        
    assert "WHALE BUY SIGNAL: 150.00 SOL moved FROM Binance to wallet" in caplog.text

def test_process_transaction_under_threshold(caplog):
    watcher, exchange_pk = build_watcher()
    watcher.client = MagicMock()
    
    mock_tx_resp = MagicMock()
    mock_tx_resp.value = MagicMock()
    mock_tx_resp.value.transaction.meta.err = None
    mock_tx_resp.value.transaction.meta.pre_balances = [10_000_000_000] # 10 SOL
    mock_tx_resp.value.transaction.meta.post_balances = [60_000_000_000] # 60 SOL (diff +50)
    
    mock_tx_resp.value.transaction.transaction.message.account_keys = [exchange_pk]
    
    watcher.client.get_transaction.return_value = mock_tx_resp
    
    sig = Signature.default()
    
    with caplog.at_level(logging.INFO):
        watcher._process_transaction(sig, exchange_pk, "Binance")
        
    assert "WHALE SELL SIGNAL" not in caplog.text
    assert "WHALE BUY SIGNAL" not in caplog.text
