from __future__ import annotations

import json
import sys

from src.analysis.paper_settlement import paper_settlement_report, run_paper_settlement
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.sqlite_utils import closing_connection


def _seed(conn, *, position_id=1, opportunity_id="opp-1", side="yes", raw=None, status="open"):
    raw = raw or {}
    conn.execute(
        """
        INSERT INTO paper_orders
        (id, run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json)
        VALUES (?, 1, ?, 'test_strategy', ?, ?, 'Test Market', 'BTC', 0.5, 2, 'filled', ?)
        """,
        (position_id, opportunity_id, side, opportunity_id, json.dumps(raw)),
    )
    conn.execute(
        """
        INSERT INTO paper_positions
        (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price, unrealized_pnl)
        VALUES (?, ?, ?, 'test_strategy', ?, 'Test Market', 'BTC', ?, '2026-06-13T10:00:00Z', 0.5, 4, 2, ?, 0.5, 0)
        """,
        (position_id, position_id, opportunity_id, opportunity_id, side, status),
    )


def test_win_settlement_from_outcome(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed(conn, raw={"status": "resolved", "outcome": "yes"})

    result = run_paper_settlement(db_path, run_id=10)

    assert result["positions_settled"] == 1
    assert result["positions_unresolved"] == 0
    assert result["details"][0]["pnl"] == 2
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT status, exit_price, realized_pnl, roi FROM paper_positions WHERE paper_position_id=1").fetchone()
        settlement = conn.execute("SELECT reason FROM paper_settlements WHERE paper_position_id=1").fetchone()
    assert row["status"] == "closed"
    assert row["exit_price"] == 1
    assert row["realized_pnl"] == 2
    assert row["roi"] == 1
    assert settlement["reason"] == "resolved_won"


def test_loss_settlement_from_outcome(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed(conn, side="yes", raw={"status": "resolved", "winner": "no"})

    result = run_paper_settlement(db_path)

    assert result["positions_settled"] == 1
    assert result["details"][0]["reason"] == "resolved_lost"
    assert result["details"][0]["pnl"] == -2


def test_unresolved_closed_missing_outcome(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed(conn, raw={"status": "closed"})

    result = run_paper_settlement(db_path)

    assert result["positions_settled"] == 0
    assert result["positions_unresolved"] == 1
    assert result["reasons"] == {"missing_outcome": 1}
    assert paper_settlement_report(db_path)["unresolved_reasons"]["missing_outcome"] == 1


def test_market_still_open(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed(conn, raw={"status": "open", "current_price": 0.55})

    result = run_paper_settlement(db_path)

    assert result["positions_settled"] == 0
    assert result["reasons"] == {"market_still_open": 1}


def test_explicit_settlement_price_and_equity_update(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed(conn, raw={"settlement_price": 0.75})

    result = run_paper_settlement(db_path, run_id=5)

    assert result["positions_settled"] == 1
    assert result["details"][0]["pnl"] == 1
    with closing_connection(db_path) as conn:
        equity = conn.execute("SELECT equity, realized_pnl FROM paper_equity_curve ORDER BY id DESC LIMIT 1").fetchone()
    assert equity["equity"] == 101
    assert equity["realized_pnl"] == 1


def test_duplicate_settlement_protection(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed(conn, raw={"settlement_price": 0.75})

    first = run_paper_settlement(db_path)
    second = run_paper_settlement(db_path)

    assert first["positions_settled"] == 1
    assert second["open_positions_checked"] == 0
    with closing_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_settlements").fetchone()[0] == 1


def test_paper_settlement_run_cli_json(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"open_positions_checked": 1, "positions_settled": 1}
    monkeypatch.setattr("src.cli.run_paper_settlement", lambda db_path: expected)
    sys.argv = ["polylens", "paper-settlement-run", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected


def test_paper_settlement_report_cli_json(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"open_positions": 0, "settlements_recorded": 1}
    monkeypatch.setattr("src.cli.paper_settlement_report", lambda db_path: expected)
    sys.argv = ["polylens", "paper-settlement-report", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected
