from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from src.analysis.paper_position_audit import build_paper_position_audit
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.sqlite_utils import closing_connection


def _seed_order(conn, order_id: int, opportunity_id: str, raw: dict):
    conn.execute(
        """
        INSERT INTO paper_orders
        (id, run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json)
        VALUES (?, 1, ?, 'short_crypto_paper', 'yes', ?, 'Bitcoin Up or Down', 'BTC', 0.5, 2, 'filled', ?)
        """,
        (order_id, opportunity_id, opportunity_id, json.dumps(raw)),
    )


def _seed_position(conn, position_id: int, opportunity_id: str, status: str = "open"):
    conn.execute(
        """
        INSERT INTO paper_positions
        (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price, unrealized_pnl)
        VALUES (?, ?, ?, 'short_crypto_paper', ?, 'Bitcoin Up or Down', 'BTC', 'yes', '2026-06-13T10:00:00Z', 0.5, 4, 2, ?, 0.5, 0)
        """,
        (position_id, position_id, opportunity_id, opportunity_id, status),
    )


def _seed_run(conn, run_id: int, opened: int = 0, settled: int = 0):
    conn.execute(
        """
        INSERT INTO paper_runs
        (id, run_timestamp, opportunities_seen, orders_created, positions_opened, positions_settled, equity, raw_json)
        VALUES (?, ?, 10, ?, ?, ?, 100, '{}')
        """,
        (run_id, f"2026-06-13T10:0{run_id}:00Z", opened, opened, settled),
    )


def test_position_audit_reports_open_positions_and_exit_reasons(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed_run(conn, 1, opened=1)
        _seed_order(conn, 1, "opp-1", {"title": "Bitcoin Up or Down"})
        _seed_position(conn, 1, "opp-1")

    report = build_paper_position_audit(
        db_path,
        opportunities=[{"opportunity_id": "opp-1", "eligible_for_paper_trading": True}],
        now=datetime(2026, 6, 13, 11, tzinfo=timezone.utc),
    )

    assert report["open_positions"] == 1
    assert report["positions_eligible_for_exit"] == 0
    assert report["positions"][0]["age_minutes"] == 60
    assert report["positions"][0]["strategy"] == "short_crypto_paper"
    assert "missing close-price source" in report["positions"][0]["reasons_not_settling"]
    assert "unresolved outcome" in report["positions"][0]["reasons_not_settling"]
    assert "no settlement evaluator" in report["root_causes"]


def test_opportunity_flow_duplicates_and_rejections(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed_run(conn, 1, opened=1)
        _seed_order(conn, 1, "opp-1", {})
        _seed_position(conn, 1, "opp-1")

    report = build_paper_position_audit(
        db_path,
        opportunities=[
            {"opportunity_id": "opp-1", "eligible_for_paper_trading": True},
            {"opportunity_id": "opp-2", "eligible_for_paper_trading": False},
        ],
    )

    assert report["duplicate_opportunities"] == 1
    assert report["rejected_opportunities"] == 2
    assert report["rejection_reasons"]["duplicate opportunity suppression"] == 1
    assert report["rejection_reasons"]["not eligible for paper trading"] == 1
    assert "duplicate opportunity suppression" in report["root_causes"]


def test_service_behavior_idle_runs(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        for run_id in range(1, 6):
            _seed_run(conn, run_id)

    report = build_paper_position_audit(db_path, opportunities=[])

    assert report["service_behavior"]["runs_completed"] == 5
    assert report["service_behavior"]["runs_with_new_entries"] == 0
    assert report["service_behavior"]["runs_with_exits"] == 0
    assert report["service_behavior"]["runs_doing_nothing"] == 5
    assert "service loop is idling" in report["root_causes"]


def test_position_with_target_price_is_exit_eligible(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed_run(conn, 1, opened=1)
        _seed_order(conn, 1, "opp-1", {"target_price": 0.75})
        _seed_position(conn, 1, "opp-1")

    report = build_paper_position_audit(db_path, opportunities=[])

    assert report["positions_eligible_for_exit"] == 1
    assert report["positions"][0]["reasons_not_settling"] == []


def test_paper_position_audit_cli_json(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"open_positions": 4, "root_causes": ["duplicate opportunity suppression"]}
    monkeypatch.setattr("src.cli.build_paper_position_audit", lambda db_path: expected)
    sys.argv = ["polylens", "paper-position-audit", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected
