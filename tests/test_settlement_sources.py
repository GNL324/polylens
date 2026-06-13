from __future__ import annotations

import json
import sqlite3
import sys

from src.analysis.paper_settlement import run_paper_settlement
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.analysis.settlement_sources import (
    get_alert_outcome,
    get_report_outcome,
    get_short_crypto_outcome,
    resolve_opportunity_outcome,
    settlement_source_audit,
)
from src.sqlite_utils import closing_connection


def _short_crypto_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            signal_id INTEGER,
            market TEXT,
            ticker TEXT,
            slug TEXT,
            token_id TEXT,
            direction TEXT,
            status TEXT,
            raw_json TEXT
        );
        CREATE TABLE paper_settlements (
            id INTEGER PRIMARY KEY,
            trade_id INTEGER,
            result TEXT,
            payout REAL,
            pnl REAL,
            roi REAL,
            settled_at TEXT,
            settlement_source TEXT,
            raw_json TEXT
        );
        """
    )
    conn.execute("INSERT INTO paper_trades VALUES (1, 27, 'm1', 'm1', 'slug1', 'token1', 'up', 'won', '{}')")
    conn.execute(
        "INSERT INTO paper_settlements (trade_id, result, payout, pnl, roi, settled_at, settlement_source, raw_json) VALUES (1, 'won', 1.0, 0.5, 1.0, '2026-06-13T00:00:00Z', 'test', ?)",
        (json.dumps({"resolved_outcome": "Up", "payout": 1.0}),),
    )
    conn.execute("INSERT INTO paper_trades VALUES (2, 28, 'm2', 'm2', 'slug2', 'token2', 'down', 'lost', '{}')")
    conn.commit()
    conn.close()


def _paper_db(path, opportunity_id="27"):
    init_paper_trading_db(path)
    with closing_connection(path) as conn:
        conn.execute(
            """
            INSERT INTO paper_orders
            (id, run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json)
            VALUES (1, 1, ?, 'short_crypto_paper', 'up', 'm1', 'BTC Up', 'BTC', 0.5, 2, 'filled', '{}')
            """,
            (opportunity_id,),
        )
        conn.execute(
            """
            INSERT INTO paper_positions
            (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price, unrealized_pnl)
            VALUES (1, 1, ?, 'short_crypto_paper', 'm1', 'BTC Up', 'BTC', 'up', '2026-06-13T00:00:00Z', 0.5, 4, 2, 'open', 0.5, 0)
            """,
            (opportunity_id,),
        )


def test_short_crypto_outcome_won(tmp_path):
    db_path = tmp_path / "short.db"
    _short_crypto_db(db_path)

    outcome = get_short_crypto_outcome("27", db_path=db_path)

    assert outcome["resolved"] is True
    assert outcome["winner"] == "up"
    assert outcome["settlement_price"] == 1.0


def test_short_crypto_outcome_lost(tmp_path):
    db_path = tmp_path / "short.db"
    _short_crypto_db(db_path)

    outcome = get_short_crypto_outcome("28", db_path=db_path)

    assert outcome["resolved"] is True
    assert outcome["settlement_price"] == 0.0


def test_report_outcome(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps({"items": [{"opportunity_id": "abc", "winner": "up", "settlement_price": 1.0}]}), encoding="utf-8")

    outcome = get_report_outcome("abc", report_glob=str(report_dir / "*.json"))

    assert outcome["resolved"] is True
    assert outcome["source"] == "report_files"
    assert outcome["winner"] == "up"


def test_alert_outcome(tmp_path):
    db_path = tmp_path / "alerts.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE alert_events (id INTEGER PRIMARY KEY, created_at TEXT, raw_json TEXT)")
    conn.execute("INSERT INTO alert_events (created_at, raw_json) VALUES ('2026-06-13T00:00:00Z', ?)", (json.dumps({"candidate": {"opportunity_id": "abc", "outcome": "yes"}}),))
    conn.commit()
    conn.close()

    outcome = get_alert_outcome("abc", db_path=db_path)

    assert outcome["resolved"] is True
    assert outcome["source"] == "alerts"


def test_resolver_priority_short_crypto(monkeypatch):
    monkeypatch.setattr("src.analysis.settlement_sources.get_short_crypto_outcome", lambda opportunity_id: {"resolved": True, "settlement_price": 1.0, "winner": "up"})
    monkeypatch.setattr("src.analysis.settlement_sources.get_report_outcome", lambda opportunity_id: {"resolved": True, "settlement_price": 0.0, "winner": "down"})

    outcome = resolve_opportunity_outcome("x")

    assert outcome["source"] == "short_crypto_paper"
    assert outcome["settlement_price"] == 1.0


def test_settlement_source_audit_counts_resolvable(monkeypatch, tmp_path):
    paper_db = tmp_path / "paper.db"
    _paper_db(paper_db, opportunity_id="27")
    monkeypatch.setattr("src.analysis.settlement_sources.resolve_opportunity_outcome", lambda opportunity_id: {"resolved": True, "winner": "up", "settlement_price": 1.0, "source": "short_crypto_paper"})
    monkeypatch.setattr("src.analysis.settlement_sources._short_crypto_resolved_count", lambda: 42)
    monkeypatch.setattr("src.analysis.settlement_sources._report_resolved_count", lambda: 11)
    monkeypatch.setattr("src.analysis.settlement_sources._alert_resolved_count", lambda: 3)

    audit = settlement_source_audit(paper_db)

    assert audit["sources"] == {"short_crypto_paper": 42, "report_files": 11, "alerts": 3}
    assert audit["resolvable_positions"] == 1
    assert audit["unresolved_positions"] == 0


def test_paper_settlement_uses_resolver(monkeypatch, tmp_path):
    paper_db = tmp_path / "paper.db"
    _paper_db(paper_db, opportunity_id="27")
    monkeypatch.setattr("src.analysis.paper_settlement.resolve_opportunity_outcome", lambda opportunity_id: {"resolved": True, "winner": "up", "settlement_price": 1.0, "source": "short_crypto_paper"})

    result = run_paper_settlement(paper_db, run_id=5)

    assert result["positions_settled"] == 1
    assert result["positions_unresolved"] == 0
    assert result["details"][0]["reason"] == "settlement_source:short_crypto_paper"
    with closing_connection(paper_db) as conn:
        row = conn.execute("SELECT status, realized_pnl FROM paper_positions WHERE paper_position_id=1").fetchone()
    assert row["status"] == "closed"
    assert row["realized_pnl"] == 2


def test_settlement_source_audit_cli_json(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"sources": {"short_crypto_paper": 1}, "resolvable_positions": 1, "unresolved_positions": 0}
    monkeypatch.setattr("src.cli.build_settlement_source_audit", lambda paper_db_path: expected)
    sys.argv = ["polylens", "settlement-source-audit", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected
