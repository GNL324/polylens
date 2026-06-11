from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "data/short_crypto_paper.db"
MIN_TRADES = 25
MAX_ADJUSTMENT = 0.10


def strategy_feedback_report(
    db_path: str = DEFAULT_DB_PATH,
    *,
    min_trades: int = MIN_TRADES,
    max_adjustment: float = MAX_ADJUSTMENT,
) -> dict[str, Any]:
    path = Path(db_path)
    min_trades = max(0, int(min_trades))
    max_adjustment = abs(float(max_adjustment))
    base_report: dict[str, Any] = {
        "status": "ok",
        "db_path": db_path,
        "min_trades": min_trades,
        "max_adjustment": max_adjustment,
        "strategies": [],
    }
    if not path.exists():
        return {**base_report, "status": "missing_db"}

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        missing_tables = _missing_tables(conn, ("paper_trades", "paper_settlements"))
        if missing_tables:
            return {
                **base_report,
                "status": "missing_tables",
                "missing_tables": missing_tables,
            }

        rows = conn.execute(
            """
            SELECT
              CASE
                WHEN t.strategy_label IS NULL OR TRIM(t.strategy_label) = '' THEN 'unknown'
                ELSE TRIM(t.strategy_label)
              END AS strategy_label,
              COUNT(*) AS closed_trades,
              AVG(COALESCE(s.roi, 0)) AS avg_roi,
              SUM(COALESCE(s.pnl, 0)) AS pnl,
              SUM(CASE WHEN s.pnl > 0 THEN 1 ELSE 0 END) AS wins,
              AVG(COALESCE(s.pnl, 0)) AS expectancy
            FROM paper_trades t
            JOIN paper_settlements s ON s.trade_id = t.id
            GROUP BY
              CASE
                WHEN t.strategy_label IS NULL OR TRIM(t.strategy_label) = '' THEN 'unknown'
                ELSE TRIM(t.strategy_label)
              END
            ORDER BY avg_roi DESC, strategy_label ASC
            """
        ).fetchall()

    strategies = []
    for row in rows:
        closed_trades = int(row["closed_trades"] or 0)
        avg_roi = float(row["avg_roi"] or 0.0)
        wins = int(row["wins"] or 0)
        win_rate = wins / closed_trades if closed_trades else None

        if closed_trades < min_trades:
            adjustment = 0.0
            eligible = False
            reason = f"needs at least {min_trades} closed trades"
        else:
            raw_adjustment = avg_roi
            adjustment = max(-max_adjustment, min(max_adjustment, raw_adjustment))
            eligible = True
            reason = "eligible"

        strategies.append(
            {
                "strategy_label": row["strategy_label"],
                "closed_trades": closed_trades,
                "win_rate": win_rate,
                "avg_roi": avg_roi,
                "pnl": float(row["pnl"] or 0.0),
                "expectancy": float(row["expectancy"] or 0.0),
                "eligible": eligible,
                "score_adjustment": adjustment,
                "reason": reason,
            }
        )

    return {**base_report, "strategies": strategies}


def _missing_tables(conn: sqlite3.Connection, expected: tuple[str, ...]) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    existing = {str(row["name"]) for row in rows}
    return [table for table in expected if table not in existing]
