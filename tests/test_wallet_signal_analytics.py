from __future__ import annotations

import json
import sqlite3

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.strategy_classifier import StrategyClassifier, init_strategy_profiles_db
from src.intelligence.wallet_signal_analytics import (
    archetype_performance_report,
    top_copied_wallets_report,
    wallet_performance_rows,
    wallet_signal_analytics_report,
)
from src.intelligence.wallet_signal_integration import link_profile_to_registry, sync_profiles_to_registry
from src.intelligence.wallet_tracker import WalletTracker

WALLET = "0x" + "a1" * 20


def _report():
    return {
        "wallet": WALLET,
        "classification": "arbitrage_trader",
        "confidence": 0.9,
        "watch_score": 88,
        "metrics": {
            "trade_count": 90,
            "markets_traded": 35,
            "overlap_count": 15,
            "overlap_ratio": 0.4,
            "merge_count": 10,
            "redeem_count": 3,
            "buy_volume": 3200,
            "sell_volume": 900,
            "btc_volume": 2500,
            "eth_volume": 700,
            "sol_volume": 0,
            "arbitrage_opportunities": 8,
            "arbitrage_volume": 600,
            "average_sum_price": 0.95,
        },
        "signals": [],
    }


def _seed_paper_copy(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE paper_copy_positions (
            paper_position_id INTEGER PRIMARY KEY,
            source_wallet TEXT,
            entry_event_key TEXT UNIQUE,
            market_id TEXT,
            market_title TEXT,
            asset TEXT,
            side TEXT,
            entry_timestamp REAL,
            entry_price REAL,
            shares REAL,
            notional REAL,
            status TEXT,
            exit_timestamp REAL,
            exit_price REAL,
            pnl REAL,
            roi REAL
        );
        INSERT INTO paper_copy_positions VALUES
        (1, ?, 'e1', 'm1', 'BTC Up', 'BTC', 'up', 1.0, 0.5, 10, 5, 'closed', 2.0, 0.7, 1.0, 0.2);
        """
    )
    conn.execute("UPDATE paper_copy_positions SET source_wallet=?", (WALLET,))
    conn.commit()
    conn.close()


def test_wallet_performance_rows_include_archetype_and_roi(tmp_path):
    traders_db = tmp_path / "traders.db"
    paper_db = tmp_path / "paper_copy.db"
    signal_db = tmp_path / "signals.db"

    save_wallet_report(_report(), db_path=str(traders_db))
    classifier = StrategyClassifier(traders_db_path=traders_db)
    profile = classifier.classify_wallet(WALLET)
    classifier.persist_profile(profile)
    _seed_paper_copy(paper_db)

    rows = wallet_performance_rows(
        paper_copy_db_path=paper_db,
        traders_db_path=traders_db,
        signal_db_path=signal_db,
    )
    assert len(rows) == 1
    assert rows[0]["wallet"] == WALLET
    assert rows[0]["archetype"] == "ARB_HUNTER"
    assert rows[0]["roi"] == 0.2
    assert rows[0]["win_rate"] == 1.0


def test_archetype_performance_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    paper_db = tmp_path / "paper_copy.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    classifier = StrategyClassifier(traders_db_path=traders_db)
    classifier.persist_profile(classifier.classify_wallet(WALLET))
    _seed_paper_copy(paper_db)

    report = archetype_performance_report(
        paper_copy_db_path=paper_db,
        traders_db_path=traders_db,
        signal_db_path=tmp_path / "signals.db",
    )
    assert report["paper_only"] is True
    assert report["archetypes"][0]["archetype"] == "ARB_HUNTER"


def test_top_copied_wallets_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    paper_db = tmp_path / "paper_copy.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    _seed_paper_copy(paper_db)

    report = top_copied_wallets_report(
        paper_copy_db_path=paper_db,
        traders_db_path=traders_db,
        signal_db_path=tmp_path / "signals.db",
        limit=5,
    )
    assert report["count"] == 1
    assert report["wallets"][0]["wallet"] == WALLET


def test_link_profile_to_registry(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    classifier = StrategyClassifier(traders_db_path=traders_db)
    profile = classifier.classify_wallet(WALLET)
    link_profile_to_registry(profile, traders_db_path=traders_db)

    from src.analysis.trader_registry import load_wallet_report

    record = load_wallet_report(WALLET, db_path=str(traders_db))
    report = record.to_dict()["report"]
    assert report["strategy_archetype"] == "ARB_HUNTER"


def test_wallet_tracker_sync_watchlist_to_paper_copy(tmp_path):
    traders_db = tmp_path / "traders.db"
    paper_db = tmp_path / "paper_copy.db"
    save_wallet_report(_report(), db_path=str(traders_db))

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=paper_db,
    )
    tracker.persist_watchlist()
    result = tracker.sync_watchlist_to_paper_copy(limit=5)
    assert result["synced"] >= 1
    assert WALLET in result["wallets"]

    conn = sqlite3.connect(paper_db)
    row = conn.execute("SELECT wallet, source FROM watched_traders WHERE wallet=?", (WALLET,)).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "wallet_intelligence"


def test_wallet_signal_analytics_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    paper_db = tmp_path / "paper_copy.db"
    save_wallet_report(_report(), db_path=str(traders_db))
    classifier = StrategyClassifier(traders_db_path=traders_db)
    classifier.persist_profile(classifier.classify_wallet(WALLET))
    _seed_paper_copy(paper_db)

    report = wallet_signal_analytics_report(
        paper_copy_db_path=paper_db,
        traders_db_path=traders_db,
        signal_db_path=tmp_path / "signals.db",
    )
    assert report["read_only"] is True
    assert report["summary"]["closed_positions"] == 1
    assert len(report["top_copied_wallets"]) == 1
