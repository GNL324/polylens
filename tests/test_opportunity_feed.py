from __future__ import annotations

import json
import sqlite3
import sys

from src.analysis.opportunity_feed import (
    get_live_opportunities,
    get_paper_trading_opportunities,
    opportunity_feed_status,
)
from src.analysis.paper_trading_engine import collect_opportunities


def _create_feed_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            sport TEXT,
            player TEXT,
            market_type TEXT,
            line REAL,
            over_book TEXT,
            under_book TEXT,
            over_odds REAL,
            under_odds REAL,
            implied_probability_sum REAL,
            guaranteed_roi REAL,
            guaranteed_profit_amount REAL,
            ranking_score REAL,
            opportunity_key TEXT,
            raw_json TEXT
        );
        CREATE TABLE prop_arbitrage_opportunities (
            id INTEGER PRIMARY KEY,
            discovered_at TEXT,
            scan_run_id INTEGER,
            player TEXT,
            sport TEXT,
            prop_type TEXT,
            line REAL,
            over_book TEXT,
            over_odds REAL,
            under_book TEXT,
            under_odds REAL,
            implied_probability_sum REAL,
            guaranteed_roi REAL,
            guaranteed_profit_amount REAL,
            ranking_score REAL,
            stake_over REAL,
            stake_under REAL,
            opportunity_type TEXT,
            status TEXT,
            raw_json TEXT
        );
        CREATE TABLE alert_events (
            id INTEGER PRIMARY KEY,
            created_at TEXT,
            alert_type TEXT,
            channel TEXT,
            opportunity_id TEXT,
            player TEXT,
            market_title TEXT,
            roi REAL,
            profit REAL,
            status TEXT,
            reason TEXT,
            raw_json TEXT
        );
        CREATE TABLE crypto_signals (
            id INTEGER PRIMARY KEY,
            market_id TEXT,
            asset TEXT,
            side TEXT,
            price REAL,
            score REAL,
            raw_json TEXT
        );
        """
    )
    live_candidate = {
        "polymarket_id": "pm-live-1",
        "polymarket_title": "Will BTC close above 70000?",
        "asset": "BTC",
        "side": "yes",
        "polymarket_implied_yes_price": 0.44,
        "estimated_edge": 0.08,
        "execution_score": 0.82,
    }
    conn.execute(
        "INSERT INTO alert_events (created_at, alert_type, channel, market_title, roi, status, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-13T00:00:00Z", "live_arbitrage", "console", "Will BTC close above 70000?", 0.08, "sent", json.dumps({"candidate": live_candidate})),
    )
    conn.execute(
        "INSERT INTO prop_arbitrage_opportunities (player, sport, prop_type, line, guaranteed_roi, ranking_score, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Jane Doe", "NBA", "player_points", 20.5, 0.04, 12.0, json.dumps({"id": "prop-1", "title": "Jane Doe points", "entry_price": 0.5})),
    )
    conn.execute(
        "INSERT INTO crypto_signals (market_id, asset, side, price, score, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
        ("crypto-1", "BTC", "up", 0.51, 77, json.dumps({"title": "Bitcoin Up or Down", "estimated_roi": 0.03})),
    )
    conn.commit()
    conn.close()


def test_live_opportunities_aggregate_normalize_dedupe_and_rank(tmp_path):
    db_path = tmp_path / "feed.db"
    _create_feed_db(db_path)

    rows = get_live_opportunities(db_paths=(str(db_path),), report_globs=(), include_ranker=False, include_auxiliary=False)

    assert len(rows) == 3
    assert rows[0]["ranking_score"] >= rows[-1]["ranking_score"]
    assert {row["source"] for row in rows} == {"live_arbitrage", "prop_arbitrage", "short_crypto"}
    assert all("entry_price" in row for row in rows)


def test_paper_trading_opportunities_filters_eligible(tmp_path):
    db_path = tmp_path / "feed.db"
    _create_feed_db(db_path)

    rows = get_paper_trading_opportunities(db_paths=(str(db_path),), report_globs=(), include_auxiliary=False)

    assert len(rows) == 3
    assert all(row["eligible_for_paper_trading"] for row in rows)


def test_feed_status_counts_sources(tmp_path):
    db_path = tmp_path / "feed.db"
    _create_feed_db(db_path)

    status = opportunity_feed_status(db_paths=(str(db_path),), report_globs=(), include_auxiliary=False)

    assert status["sources"]["live_arbitrage"] == 1
    assert status["sources"]["prop_arbitrage"] == 1
    assert status["sources"]["short_crypto"] == 1
    assert status["total_opportunities"] == 3
    assert status["eligible_for_paper_trading"] == 3


def test_paper_engine_collects_from_unified_feed(monkeypatch):
    expected = [{"id": "feed-1", "entry_price": 0.5}]

    def fake_feed(limit=None):
        assert limit == 7
        return expected

    monkeypatch.setattr("src.analysis.paper_trading_engine.get_paper_trading_opportunities", fake_feed)
    assert collect_opportunities(limit=7) == expected


def test_opportunity_feed_status_cli_json(capsys, monkeypatch):
    from src.cli import main

    expected = {"sources": {"live_arbitrage": 1}, "total_opportunities": 1, "eligible_for_paper_trading": 1}
    monkeypatch.setattr("src.cli.build_opportunity_feed_status", lambda: expected)
    sys.argv = ["polylens", "opportunity-feed-status", "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected
