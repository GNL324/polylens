from __future__ import annotations

import json
import sqlite3

from src.analysis.opportunity_feed import get_live_opportunities
from src.analysis.trader_signal_paper_bridge import init_trader_signal_paper_bridge_db

WALLET = "0x" + "a" * 40


def _seed_trader_signal_intent(db_path):
    init_trader_signal_paper_bridge_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS trader_signals (
            id INTEGER PRIMARY KEY,
            signal_key TEXT UNIQUE,
            wallet TEXT,
            market_id TEXT,
            market_title TEXT,
            asset TEXT,
            side TEXT,
            signal_type TEXT,
            timestamp REAL,
            action TEXT,
            price REAL,
            shares REAL,
            amount REAL,
            score REAL,
            tx_hash TEXT,
            raw_json TEXT,
            created_at TEXT
        );
        INSERT INTO trader_signal_paper_intents (
            intent_key, recommendation_id, market_id, side, recommendation_type,
            signal_type, trader_address, score, validation_count, historical_accuracy,
            gate_status, gate_reason, notional_usd, status, reason, created_at,
            read_only, paper_only
        ) VALUES (
            'rec:simulated', 'rec', 'market-1', 'up', 'paper_entry',
            'conviction', '{WALLET}', 82.0, 25, 0.62,
            'proven', 'ok', 10.0, 'simulated', 'test', '2026-06-15T00:00:00Z',
            1, 1
        );
        INSERT INTO trader_signals (
            signal_key, wallet, market_id, market_title, asset, side, signal_type,
            timestamp, action, price, shares, amount, score, tx_hash, raw_json, created_at
        ) VALUES (
            'sig-1', '{WALLET}', 'market-1', 'BTC Up', 'BTC', 'up', 'conviction',
            1.0, 'buy', 0.55, 10.0, 5.5, 82.0, '0x1', '{{}}', '2026-06-15T00:00:00Z'
        );
        """
    )
    conn.commit()
    conn.close()


def test_opportunity_feed_includes_trader_signal_intents(tmp_path):
    signal_db = tmp_path / "trader_signals.db"
    _seed_trader_signal_intent(signal_db)

    import src.analysis.opportunity_feed as feed_module

    original = feed_module.DEFAULT_TRADER_SIGNAL_DB
    feed_module.DEFAULT_TRADER_SIGNAL_DB = str(signal_db)
    try:
        opportunities = get_live_opportunities(include_ranker=False, db_paths=(), report_globs=(), include_auxiliary=True)
    finally:
        feed_module.DEFAULT_TRADER_SIGNAL_DB = original

    trader_rows = [row for row in opportunities if row["source"] == "trader_signal"]
    assert len(trader_rows) == 1
    assert trader_rows[0]["strategy"].startswith("wallet_signal:")
    assert trader_rows[0]["eligible_for_paper_trading"] is True
    assert trader_rows[0]["entry_price"] == 0.55
