from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "data/opportunities.db"


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
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
                opportunity_key TEXT UNIQUE,
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                opportunity_key TEXT NOT NULL,
                destination TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            )
        """)


def opportunity_key(opportunity: dict[str, Any]) -> str:
    return "|".join(str(opportunity.get(field) or "") for field in ("player", "prop_type", "line", "over_book", "under_book", "over_odds", "under_odds"))


def save_opportunity(opportunity: dict[str, Any], db_path: str = DEFAULT_DB_PATH, sport: str | None = None) -> bool:
    init_db(db_path)
    key = opportunity.get("opportunity_key") or opportunity_key(opportunity)
    timestamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO opportunities (
                    timestamp, sport, player, market_type, line, over_book, under_book, over_odds, under_odds,
                    implied_probability_sum, guaranteed_roi, guaranteed_profit_amount, ranking_score, opportunity_key, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    sport or opportunity.get("sport"),
                    opportunity.get("player"),
                    opportunity.get("prop_type") or opportunity.get("market_type"),
                    opportunity.get("line"),
                    opportunity.get("over_book"),
                    opportunity.get("under_book"),
                    opportunity.get("over_odds"),
                    opportunity.get("under_odds"),
                    opportunity.get("implied_probability_sum"),
                    opportunity.get("guaranteed_roi"),
                    opportunity.get("guaranteed_profit_amount"),
                    opportunity.get("ranking_score"),
                    key,
                    json.dumps(opportunity, sort_keys=True),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def save_alert(opportunity_key_value: str, destination: str, status: str, error: str | None = None, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO alerts (timestamp, opportunity_key, destination, status, error) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), opportunity_key_value, destination, status, error),
        )


def load_recent_opportunities(limit: int = 20, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM opportunities ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def load_recent_alerts(limit: int = 20, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def opportunity_stats(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        avg_roi = conn.execute("SELECT AVG(guaranteed_roi) FROM opportunities").fetchone()[0]
        best_roi = conn.execute("SELECT MAX(guaranteed_roi) FROM opportunities").fetchone()[0]
        pair = conn.execute("SELECT over_book || ' / ' || under_book AS pair, COUNT(*) c FROM opportunities GROUP BY pair ORDER BY c DESC LIMIT 1").fetchone()
        by_sport = conn.execute("SELECT COALESCE(sport, 'unknown') sport, COUNT(*) c FROM opportunities GROUP BY sport").fetchall()
    return {
        "total_opportunities": total,
        "average_roi": avg_roi or 0,
        "best_roi": best_roi or 0,
        "most_common_bookmaker_pair": pair[0] if pair else None,
        "opportunities_by_sport": {sport: count for sport, count in by_sport},
    }
