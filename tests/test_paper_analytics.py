from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from src.analysis.paper_analytics import build_paper_analytics_report
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.sqlite_utils import closing_connection


def _seed_position(conn, *, position_id, strategy, asset, status, notional, realized=0.0, unrealized=0.0, entry="2026-06-13T10:00:00Z", exit_time=None):
    conn.execute(
        """
        INSERT INTO paper_positions
        (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price, exit_timestamp, exit_price, realized_pnl, unrealized_pnl, roi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position_id,
            position_id,
            f"opp-{position_id}",
            strategy,
            f"market-{position_id}",
            "Test Market",
            asset,
            "yes",
            entry,
            0.5,
            4,
            notional,
            status,
            0.5,
            exit_time,
            0.75 if status == "closed" else None,
            realized if status == "closed" else None,
            unrealized,
            (realized / notional) if status == "closed" and notional else None,
        ),
    )


def test_paper_analytics_report_populated(tmp_path):
    db_path = tmp_path / "paper.db"
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        _seed_position(conn, position_id=1, strategy="short_crypto_paper", asset="BTC", status="closed", notional=2, realized=1, exit_time="2026-06-13T11:00:00Z")
        _seed_position(conn, position_id=2, strategy="short_crypto_paper", asset="BTC", status="closed", notional=2, realized=-0.5, exit_time="2026-06-13T12:00:00Z")
        _seed_position(conn, position_id=3, strategy="live_arbitrage", asset="ETH", status="open", notional=2, unrealized=0.25)
        conn.execute("INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason) VALUES (1, 1, '2026-06-13T11:00:00Z', 0.75, 1, 0.5, 'test')")
        conn.execute("INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason) VALUES (1, 2, '2026-06-13T12:00:00Z', 0.25, -0.5, -0.25, 'test')")
        conn.execute("INSERT INTO paper_equity_curve (run_id, timestamp, equity, realized_pnl, unrealized_pnl, open_positions, drawdown) VALUES (1, '2026-06-13T10:00:00Z', 100, 0, 0, 1, 0)")
        conn.execute("INSERT INTO paper_equity_curve (run_id, timestamp, equity, realized_pnl, unrealized_pnl, open_positions, drawdown) VALUES (2, '2026-06-13T11:00:00Z', 101, 1, 0, 1, 0)")
        conn.execute("INSERT INTO paper_equity_curve (run_id, timestamp, equity, realized_pnl, unrealized_pnl, open_positions, drawdown) VALUES (3, '2026-06-13T12:00:00Z', 100.5, 0.5, 0, 1, -0.5)")

    report = build_paper_analytics_report(db_path, now=datetime(2026, 6, 13, 13, tzinfo=timezone.utc))

    assert report["equity"]["current"] == 100.75
    assert report["equity"]["net_pnl"] == 0.75
    assert report["equity"]["roi"] == 0.0075
    assert report["trading"]["total_trades"] == 3
    assert report["trading"]["open_trades"] == 1
    assert report["trading"]["closed_trades"] == 2
    assert report["trading"]["win_rate"] == 0.5
    assert report["trading"]["loss_rate"] == 0.5
    assert report["trading"]["profit_factor"] == 2
    assert report["risk"]["max_drawdown"] == 0.5
    assert report["risk"]["longest_drawdown"] == 1
    assert report["strategy_breakdown"]["short_crypto_paper"]["pnl"] == 0.5
    assert report["strategy_breakdown"]["short_crypto_paper"]["win_rate"] == 0.5
    assert report["strategy_breakdown"]["live_arbitrage"]["exposure"] == 2
    assert report["asset_breakdown"]["BTC"]["closed_trades"] == 2
    assert report["asset_breakdown"]["ETH"]["open_trades"] == 1
    assert report["time_analysis"]["pnl_by_day"]["2026-06-13"] == 0.5
    assert report["time_analysis"]["pnl_last_24h"] == 0.5
    assert report["best_strategy"]["name"] == "short_crypto_paper"


def test_paper_analytics_empty_db_is_stable(tmp_path):
    report = build_paper_analytics_report(tmp_path / "empty.db")

    assert report["equity"]["current"] == 100
    assert report["equity"]["roi"] == 0
    assert report["trading"]["total_trades"] == 0
    assert report["risk"]["max_drawdown"] == 0
    assert report["best_strategy"] is None


def test_paper_analytics_cli_json_output(capsys, monkeypatch, tmp_path):
    from src.cli import main

    expected = {"equity": {"current": 104.72}, "trading": {"closed_trades": 53}}

    def fake_report(db_path):
        return expected

    monkeypatch.setattr("src.cli.build_paper_analytics_report", fake_report)
    sys.argv = ["polylens", "paper-analytics-report", "--db-path", str(tmp_path / "paper.db"), "--json"]
    main()
    assert json.loads(capsys.readouterr().out) == expected
