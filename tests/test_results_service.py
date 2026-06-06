import sqlite3

from src.services.results_service import fetch_trades, initialize_results_store, summarize_results
from src.web.db import rows_or_empty


def test_results_summary_keeps_paper_and_live_separate(tmp_path) -> None:
    db_path = tmp_path / "polylens.db"
    initialize_results_store(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO trades
            (mode, opened_at, closed_at, platform, market_id, market_title, strategy, market_type, side,
             entry_price, exit_price, stake, quantity, gross_pnl, fees, net_pnl, roi, status, notes, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("paper", "2026-01-01", "2026-01-02", "kalshi", "m1", "A", "s1", "event", "yes", 0.4, 0.6, 100, 250, 50, 2, 48, 0.48, "closed", "", "{}"),
                ("paper", "2026-01-03", None, "kalshi", "m2", "B", "s1", "event", "yes", 0.5, None, 40, 80, 0, 0, -4, -0.1, "open", "", "{}"),
                ("live", "2026-01-04", "2026-01-05", "polymarket", "m3", "C", "s2", "sports", "no", 0.7, 0.8, 50, 70, 10, 1, 9, 0.18, "closed", "", "{}"),
                ("live", "2026-01-06", None, "polymarket", "m4", "D", "s2", "sports", "no", 0.3, None, 20, 60, 0, 0, 3, 0.15, "open", "", "{}"),
            ],
        )

    summary = summarize_results(db_path)
    assert summary["paper_realized_pnl"] == 48
    assert summary["paper_unrealized_pnl"] == -4
    assert summary["live_realized_pnl"] == 9
    assert summary["live_unrealized_pnl"] == 3
    assert summary["total_volume"] == 210
    assert summary["fees"] == 3
    assert summary["net_pnl"] == 56
    assert summary["win_rate"] == 1
    assert summary["open_positions"] == 2
    assert summary["closed_trades"] == 2


def test_missing_table_handling(tmp_path) -> None:
    db_path = tmp_path / "empty.db"
    assert rows_or_empty(db_path, "scan_runs", "SELECT * FROM scan_runs") == []
    assert fetch_trades(db_path) == []
    summary = summarize_results(db_path)
    assert summary["net_pnl"] == 0
    assert summary["closed_trades"] == 0

