import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import wallet_analyzer
from src.wallet_analyzer import WalletAnalyzer, render_trade_journal


WALLET = "11111111111111111111111111111111"
JUP_DECIMALS = 6
USDC_DECIMALS = 6


def _token_balance(account_index: int, mint: str, owner: str, amount_raw: int, decimals: int) -> dict:
    return {
        "accountIndex": account_index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {
            "amount": str(amount_raw),
            "decimals": decimals,
            "uiAmountString": str(amount_raw / (10**decimals)),
        },
    }


def _transaction(
    *,
    signature: str,
    block_time: int,
    slot: int,
    pre_lamports: int,
    post_lamports: int,
    fee_lamports: int,
    pre_token_balances: list[dict],
    post_token_balances: list[dict],
    logs: list[str] | None = None,
) -> dict:
    return {
        "slot": slot,
        "blockTime": block_time,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [
                    {"pubkey": WALLET},
                    {"pubkey": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                    {"pubkey": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"},
                    {"pubkey": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
                ]
            },
        },
        "meta": {
            "err": None,
            "fee": fee_lamports,
            "preBalances": [pre_lamports, 0, 0, 0],
            "postBalances": [post_lamports, 0, 0, 0],
            "preTokenBalances": pre_token_balances,
            "postTokenBalances": post_token_balances,
            "logMessages": logs or [],
        },
    }


def test_parse_swap_excludes_network_fee_from_native_sol_leg():
    analyzer = WalletAnalyzer(WALLET)
    tx = _transaction(
        signature="sig-buy-sol",
        block_time=1_710_000_000,
        slot=100,
        pre_lamports=10_000_005_000,
        post_lamports=9_000_000_000,
        fee_lamports=5_000,
        pre_token_balances=[],
        post_token_balances=[
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            )
        ],
    )

    swap = analyzer.parse_swap(tx)

    assert swap is not None
    assert swap.sold.mint == wallet_analyzer.SOL_MINT
    assert swap.sold.amount == pytest.approx(1.0)
    assert swap.bought.mint == wallet_analyzer.JUP_MINT
    assert swap.bought.amount == pytest.approx(100.0)
    assert swap.fee_sol == pytest.approx(0.000005)


def test_analyze_from_transactions_matches_fifo_and_aggregates_realized_pnl():
    analyzer = WalletAnalyzer(WALLET)
    buy_tx = _transaction(
        signature="sig-buy-usdc",
        block_time=1_710_000_000,
        slot=101,
        pre_lamports=1_000_000_000,
        post_lamports=999_995_000,
        fee_lamports=5_000,
        pre_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                100 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            )
        ],
        post_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                70 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            ),
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            ),
        ],
    )
    sell_tx = _transaction(
        signature="sig-sell-usdc",
        block_time=1_710_086_400,
        slot=102,
        pre_lamports=999_995_000,
        post_lamports=999_990_000,
        fee_lamports=5_000,
        pre_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                70 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            ),
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            ),
        ],
        post_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                110 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            )
        ],
    )

    analysis = analyzer.analyze_from_transactions(
        [buy_tx, sell_tx], fetched_signatures=2
    )

    assert analysis.swap_count == 2
    assert analysis.total_fee_sol == pytest.approx(0.00001)
    assert len(analysis.closed_trades) == 1

    trade = analysis.closed_trades[0]
    assert trade.token_symbol == "JUP"
    assert trade.cost_currency == "USD"
    assert trade.proceeds_currency == "USD"
    assert trade.realized_pnl == pytest.approx(10.0)
    assert trade.return_pct == pytest.approx(33.3333333333)
    assert trade.hold_seconds == pytest.approx(86_400.0)

    summary = analysis.token_summaries[0]
    assert summary.symbol == "JUP"
    assert summary.closed_trades == 1
    assert summary.comparable_trades == 1
    assert summary.unresolved_trades == 0
    assert summary.realized_pnl_by_currency == {"USD": pytest.approx(10.0)}
    assert summary.average_hold_seconds == pytest.approx(86_400.0)
    assert summary.open_quantity == pytest.approx(0.0)
    assert analysis.best_trade == trade
    assert analysis.worst_trade == trade


def test_cross_quote_closure_is_flagged_as_unresolved_instead_of_force_converted():
    analyzer = WalletAnalyzer(WALLET)
    buy_tx = _transaction(
        signature="sig-buy-sol",
        block_time=1_710_000_000,
        slot=201,
        pre_lamports=10_000_005_000,
        post_lamports=9_000_000_000,
        fee_lamports=5_000,
        pre_token_balances=[],
        post_token_balances=[
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            )
        ],
    )
    sell_tx = _transaction(
        signature="sig-sell-usdc",
        block_time=1_710_086_400,
        slot=202,
        pre_lamports=9_000_000_000,
        post_lamports=8_999_995_000,
        fee_lamports=5_000,
        pre_token_balances=[
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            )
        ],
        post_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                40 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            )
        ],
    )

    analysis = analyzer.analyze_from_transactions([buy_tx, sell_tx], fetched_signatures=2)

    assert len(analysis.closed_trades) == 1
    trade = analysis.closed_trades[0]
    assert trade.status == "cross_quote"
    assert trade.realized_pnl is None
    assert trade.return_pct is None
    assert len(analysis.unresolved_closed_trades) == 1
    summary = analysis.token_summaries[0]
    assert summary.comparable_trades == 0
    assert summary.unresolved_trades == 1


def test_render_trade_journal_includes_summary_and_lessons():
    analyzer = WalletAnalyzer(WALLET)
    buy_tx = _transaction(
        signature="sig-buy-usdc",
        block_time=1_710_000_000,
        slot=301,
        pre_lamports=1_000_000_000,
        post_lamports=999_995_000,
        fee_lamports=5_000,
        pre_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                100 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            )
        ],
        post_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                70 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            ),
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            ),
        ],
    )
    sell_tx = _transaction(
        signature="sig-sell-usdc",
        block_time=1_710_086_400,
        slot=302,
        pre_lamports=999_995_000,
        post_lamports=999_990_000,
        fee_lamports=5_000,
        pre_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                70 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            ),
            _token_balance(
                2,
                wallet_analyzer.JUP_MINT,
                WALLET,
                100 * 10**JUP_DECIMALS,
                JUP_DECIMALS,
            ),
        ],
        post_token_balances=[
            _token_balance(
                3,
                wallet_analyzer.USDC_MINT,
                WALLET,
                110 * 10**USDC_DECIMALS,
                USDC_DECIMALS,
            )
        ],
    )
    analysis = analyzer.analyze_from_transactions([buy_tx, sell_tx], fetched_signatures=2)

    report = render_trade_journal(analysis)

    assert report.startswith("# Trade Journal")
    assert "| JUP | 1 | $10.00 | 24.0h | 1 | 0 | 0.000000 | - |" in report
    assert "Comparable round trips won 100.0% of the time (1/1)." in report
    assert "Network fees consumed 0.000010 SOL across 2 swaps (0.000005 SOL per swap)." in report
