from __future__ import annotations

import inspect
import sqlite3

from src.analysis import paper_portfolio
from src.analysis.paper_portfolio import (
    portfolio_report,
    polymarket_analytics_url,
    rebuild_portfolio_analytics,
    reconstruct_portfolio_value_at,
    replay_portfolio,
    strategy_attribution,
    trade_detail,
    wallet_attribution,
)
from src.analysis.paper_trading_engine import (
    PaperTradingConfig,
    record_equity_snapshot,
    run_paper_trading_engine,
    settle_open_positions,
)
from src.sqlite_utils import closing_connection


VALID_WALLET = "0x7af3f727e86394ca3986a1f786b888c7904e83fe"


def _opp(**overrides):
    row = {
        "id": "opp-1",
        "strategy": "early_entry",
        "market_id": "btc-up",
        "title": "Will BTC close above 100k?",
        "asset": "BTC",
        "side": "yes",
        "entry_price": 0.5,
        "target_price": None,
        "estimated_roi": 0.5,
        "ranking_score": 90,
        "wallet": VALID_WALLET,
        "signal_family": "early_entry",
        "confidence_score": 0.82,
    }
    row.update(overrides)
    return row


def test_ledger_balance_and_attribution_records_are_written(tmp_path):
    db_path = tmp_path / "paper.db"

    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp()]])
    settle_open_positions(db_path=db_path, run_id=2, prices={"opp-1": 0.75})
    record_equity_snapshot(db_path, run_id=2)

    with closing_connection(db_path) as conn:
        ledger = conn.execute("SELECT event_type, action, realized_pnl, wallet FROM paper_portfolio_ledger ORDER BY id").fetchall()
        snapshots = conn.execute("SELECT total_equity, realized_pnl, closed_positions FROM paper_balance_snapshots ORDER BY id").fetchall()
        attribution = conn.execute("SELECT net_pnl, strategy, wallet, exit_reason FROM paper_trade_attribution").fetchone()

    assert [row["event_type"] for row in ledger] == ["OPEN", "CLOSE"]
    assert ledger[0]["action"] == "BUY"
    assert ledger[1]["action"] == "SELL"
    assert ledger[1]["realized_pnl"] == 1
    assert ledger[1]["wallet"] == VALID_WALLET
    assert snapshots[-1]["total_equity"] == 101
    assert snapshots[-1]["realized_pnl"] == 1
    assert snapshots[-1]["closed_positions"] == 1
    assert attribution["net_pnl"] == 1
    assert attribution["strategy"] == "early_entry"
    assert attribution["wallet"] == VALID_WALLET
    assert attribution["exit_reason"] == "simulated_exit"


def test_portfolio_report_replay_and_reconstruction(tmp_path):
    db_path = tmp_path / "paper.db"

    run_paper_trading_engine(db_path=db_path, collectors=[lambda: [_opp()]])
    settle_open_positions(db_path=db_path, run_id=2, prices={"opp-1": 0.25})
    record_equity_snapshot(db_path, run_id=2)

    report = portfolio_report(db_path)
    history = replay_portfolio(db_path)
    with closing_connection(db_path) as conn:
        snapshot_timestamp = conn.execute("SELECT timestamp FROM paper_balance_snapshots ORDER BY id DESC LIMIT 1").fetchone()["timestamp"]
    reconstructed = reconstruct_portfolio_value_at(db_path, snapshot_timestamp)
    detail = trade_detail(db_path, 1)

    assert report["portfolio"]["realized_pnl"] == -1
    assert report["pnl"]["all_time"] == -1
    assert report["largest_loser"]["net_pnl"] == -1
    assert history[-1]["open_positions"] == 0
    assert reconstructed["total_equity"] == 99
    assert detail["net_pnl"] == -1


def test_wallet_and_strategy_attribution_include_rankings_and_wallet_url(tmp_path):
    db_path = tmp_path / "paper.db"
    opportunities = [
        _opp(id="win", market_id="win", strategy="early_entry", ranking_score=90),
        _opp(id="loss", market_id="loss", strategy="conviction", ranking_score=80),
    ]

    run_paper_trading_engine(db_path=db_path, collectors=[lambda: opportunities], config=PaperTradingConfig(max_strategy_exposure=1.0))
    settle_open_positions(db_path=db_path, run_id=2, prices={"win": 0.75, "loss": 0.25})

    wallets = wallet_attribution(db_path)
    strategies = strategy_attribution(db_path)

    assert wallets[0]["wallet"] == VALID_WALLET
    assert wallets[0]["polymarket_analytics_url"] == f"https://polymarketanalytics.com/traders/{VALID_WALLET}"
    assert wallets[0]["trade_count"] == 2
    assert wallets[0]["win_rate"] == 0.5
    assert {row["strategy"] for row in strategies} == {"early_entry", "conviction"}
    assert strategies[0]["total_pnl"] >= strategies[-1]["total_pnl"]
    assert strategies[0]["contribution_pct"] >= 0


def test_empty_db_report_is_safe_and_rebuild_is_noop(tmp_path):
    db_path = tmp_path / "empty.db"

    report = portfolio_report(db_path)
    rebuild = rebuild_portfolio_analytics(db_path)

    assert report["portfolio"]["total_equity"] == 100
    assert report["recent_trades"] == []
    assert report["wallet_attribution"] == []
    assert rebuild["positions_processed"] == 0


def test_wallet_url_rejects_malformed_values():
    assert polymarket_analytics_url(VALID_WALLET) == f"https://polymarketanalytics.com/traders/{VALID_WALLET}"
    assert polymarket_analytics_url("wallet-123") is None
    assert polymarket_analytics_url("0x123") is None


def test_portfolio_analytics_source_stays_paper_only():
    source = inspect.getsource(paper_portfolio)
    forbidden = [
        "submit_order",
        "place_order",
        "send_order",
        "private_key",
        "api_secret",
        "src.trading.executor",
    ]
    assert all(token not in source for token in forbidden)
