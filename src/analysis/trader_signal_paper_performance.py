from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.analysis.trader_signal_paper_bridge import init_trader_signal_paper_bridge_db
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def init_paper_strategy_performance_db(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> None:
    init_trader_signal_paper_bridge_db(db_path)
    init_trader_signal_validation_db(db_path)
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_strategy_positions (
                position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL UNIQUE,
                intent_key TEXT NOT NULL UNIQUE,
                recommendation_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                signal_family TEXT NOT NULL,
                strategy_label TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                trader_address TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                shares REAL NOT NULL,
                notional_usd REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_price REAL,
                closed_at TEXT,
                pnl REAL,
                roi REAL,
                read_only INTEGER NOT NULL DEFAULT 1,
                paper_only INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS paper_strategy_settlements (
                settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL UNIQUE,
                intent_id INTEGER NOT NULL,
                validation_key TEXT NOT NULL,
                market_id TEXT NOT NULL,
                signal_family TEXT NOT NULL,
                strategy_label TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                actual_outcome TEXT NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                roi REAL NOT NULL,
                settled_at TEXT NOT NULL,
                read_only INTEGER NOT NULL DEFAULT 1,
                paper_only INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(position_id) REFERENCES paper_strategy_positions(position_id)
            );
            CREATE TABLE IF NOT EXISTS paper_strategy_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_family TEXT NOT NULL,
                strategy_label TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                trades INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                pnl REAL NOT NULL,
                roi REAL NOT NULL,
                expectancy REAL NOT NULL,
                drawdown REAL NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(signal_family, strategy_label, recommendation_type)
            );
            CREATE TABLE IF NOT EXISTS paper_strategy_daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_date TEXT NOT NULL,
                signal_family TEXT NOT NULL,
                strategy_label TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                trades INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                pnl REAL NOT NULL,
                roi REAL NOT NULL,
                expectancy REAL NOT NULL,
                drawdown REAL NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(stat_date, signal_family, strategy_label, recommendation_type)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_strategy_positions_status ON paper_strategy_positions(status);
            CREATE INDEX IF NOT EXISTS idx_paper_strategy_positions_family ON paper_strategy_positions(signal_family, status);
            CREATE INDEX IF NOT EXISTS idx_paper_strategy_settlements_family ON paper_strategy_settlements(signal_family, settled_at);
            """
        )


def sync_paper_strategy_positions_from_intents(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    init_paper_strategy_performance_db(db_path)
    inserted = 0
    skipped = 0
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT i.intent_id, i.intent_key, i.recommendation_id, i.market_id, i.signal_type,
                   i.recommendation_type, i.trader_address, i.side, i.notional_usd, i.created_at,
                   s.signal_key, s.price
            FROM trader_signal_paper_intents i
            LEFT JOIN trader_signals s
              ON s.signal_key = ?
                 OR s.signal_key = substr(i.recommendation_id, 1, length(i.recommendation_id) - length(':' || i.recommendation_type))
            WHERE i.status = 'simulated'
            ORDER BY i.intent_id ASC
            """,
            ("__never__",),
        ).fetchall()
        for row in rows:
            entry_price = _bounded_price(row["price"])
            notional = float(row["notional_usd"] or 0.0)
            if notional <= 0:
                skipped += 1
                continue
            shares = notional / entry_price if entry_price > 0 else 0.0
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO paper_strategy_positions (
                    intent_id, intent_key, recommendation_id, signal_id, market_id,
                    signal_family, strategy_label, recommendation_type, trader_address,
                    side, entry_price, shares, notional_usd, opened_at, status,
                    read_only, paper_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 1, 1)
                """,
                (
                    int(row["intent_id"]),
                    row["intent_key"],
                    row["recommendation_id"],
                    row["signal_key"] or _signal_id_from_recommendation(row["recommendation_id"], row["recommendation_type"]),
                    row["market_id"],
                    row["signal_type"],
                    f"wallet_signal:{row['signal_type']}",
                    row["recommendation_type"],
                    row["trader_address"],
                    row["side"],
                    entry_price,
                    round(shares, 8),
                    round(notional, 6),
                    row["created_at"],
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1
    return _with_flags({"positions_inserted": inserted, "positions_skipped": skipped})


def settle_paper_strategy_positions(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    init_paper_strategy_performance_db(db_path)
    checked = 0
    settled = 0
    unresolved = 0
    with closing_connection(db_path) as conn:
        positions = conn.execute(
            """
            SELECT *
            FROM paper_strategy_positions
            WHERE status = 'open'
            ORDER BY position_id ASC
            """
        ).fetchall()
        checked = len(positions)
        for position in positions:
            validation = _find_validation(conn, position)
            if validation is None:
                unresolved += 1
                continue
            exit_price = 1.0 if int(validation["correct"] or 0) else 0.0
            pnl = (exit_price - float(position["entry_price"])) * float(position["shares"])
            roi = _ratio(pnl, float(position["notional_usd"]))
            settled_at = str(validation["resolved_at"] or _utc_now())
            conn.execute(
                """
                UPDATE paper_strategy_positions
                SET status='closed', exit_price=?, closed_at=?, pnl=?, roi=?
                WHERE position_id=? AND status='open'
                """,
                (exit_price, settled_at, round(pnl, 6), round(roi, 6), position["position_id"]),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_strategy_settlements (
                    position_id, intent_id, validation_key, market_id, signal_family,
                    strategy_label, recommendation_type, actual_outcome, exit_price,
                    pnl, roi, settled_at, read_only, paper_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
                """,
                (
                    position["position_id"],
                    position["intent_id"],
                    validation["validation_key"],
                    position["market_id"],
                    position["signal_family"],
                    position["strategy_label"],
                    position["recommendation_type"],
                    validation["outcome"],
                    exit_price,
                    round(pnl, 6),
                    round(roi, 6),
                    settled_at,
                ),
            )
            settled += 1
    return _with_flags({"positions_checked": checked, "positions_settled": settled, "positions_unresolved": unresolved})


def rebuild_paper_strategy_attribution(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    init_paper_strategy_performance_db(db_path)
    updated_at = _utc_now()
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_strategy_positions WHERE status='closed' ORDER BY closed_at ASC, position_id ASC").fetchall()
        conn.execute("DELETE FROM paper_strategy_performance")
        conn.execute("DELETE FROM paper_strategy_daily_stats")
        for key, items in _group(rows, ("signal_family", "strategy_label", "recommendation_type")).items():
            stats = _stats(items)
            conn.execute(
                """
                INSERT INTO paper_strategy_performance (
                    signal_family, strategy_label, recommendation_type, trades, wins, losses,
                    win_rate, pnl, roi, expectancy, drawdown, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*key, *stats, updated_at),
            )
        for key, items in _group(rows, ("stat_date", "signal_family", "strategy_label", "recommendation_type")).items():
            stats = _stats(items)
            conn.execute(
                """
                INSERT INTO paper_strategy_daily_stats (
                    stat_date, signal_family, strategy_label, recommendation_type, trades, wins, losses,
                    win_rate, pnl, roi, expectancy, drawdown, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*key, *stats, updated_at),
            )
    return _with_flags({"performance_rows": len(_load_table(db_path, "paper_strategy_performance")), "daily_rows": len(_load_table(db_path, "paper_strategy_daily_stats"))})


def paper_strategy_performance_report(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    init_paper_strategy_performance_db(db_path)
    sync = sync_paper_strategy_positions_from_intents(db_path)
    settlement = settle_paper_strategy_positions(db_path)
    rebuild = rebuild_paper_strategy_attribution(db_path)
    positions = _load_table(db_path, "paper_strategy_positions")
    settlements = _load_table(db_path, "paper_strategy_settlements")
    performance = _load_table(db_path, "paper_strategy_performance")
    daily = _load_table(db_path, "paper_strategy_daily_stats")
    open_positions = [row for row in positions if row["status"] == "open"]
    closed_positions = [row for row in positions if row["status"] == "closed"]
    pnl_values = [float(row["pnl"] or 0.0) for row in closed_positions]
    notional = sum(float(row["notional_usd"] or 0.0) for row in closed_positions)
    wins = sum(1 for value in pnl_values if value > 0)
    return _with_flags(
        {
            "lifecycle": "paper_intent -> paper_strategy_position -> trader_signal_validation -> paper_strategy_settlement -> pnl",
            "sync": sync,
            "settlement": settlement,
            "rebuild": rebuild,
            "summary": {
                "positions": len(positions),
                "open_positions": len(open_positions),
                "closed_positions": len(closed_positions),
                "settlements": len(settlements),
                "trades": len(closed_positions),
                "wins": wins,
                "losses": sum(1 for value in pnl_values if value < 0),
                "win_rate": round(_ratio(wins, len(closed_positions)), 6),
                "pnl": round(sum(pnl_values), 6),
                "roi": round(_ratio(sum(pnl_values), notional), 6),
                "expectancy": round(mean(pnl_values), 6) if pnl_values else 0.0,
                "drawdown": round(_max_drawdown(pnl_values), 6),
            },
            "by_strategy": performance,
            "daily_stats": daily,
        }
    )


def _find_validation(conn: Any, position: Any) -> Any | None:
    return conn.execute(
        """
        SELECT validation_key, resolved_at, outcome, correct
        FROM trader_signal_validation
        WHERE signal_id = ?
           OR (market_id = ? AND signal_type = ?)
        ORDER BY CASE WHEN signal_id = ? THEN 0 ELSE 1 END, resolved_at DESC
        LIMIT 1
        """,
        (position["signal_id"], position["market_id"], position["signal_family"], position["signal_id"]),
    ).fetchone()


def _group(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        enriched = dict(row)
        enriched["stat_date"] = str(enriched.get("closed_at") or "")[:10] or "unknown"
        grouped.setdefault(tuple(enriched[key] for key in keys), []).append(enriched)
    return grouped


def _stats(rows: list[dict[str, Any]]) -> tuple[int, int, int, float, float, float, float, float]:
    pnl_values = [float(row["pnl"] or 0.0) for row in rows]
    notional = sum(float(row["notional_usd"] or 0.0) for row in rows)
    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    pnl = round(sum(pnl_values), 6)
    return (
        len(rows),
        wins,
        losses,
        round(_ratio(wins, len(rows)), 6),
        pnl,
        round(_ratio(pnl, notional), 6),
        round(mean(pnl_values), 6) if pnl_values else 0.0,
        round(_max_drawdown(pnl_values), 6),
    )


def _load_table(db_path: str | Path, table: str) -> list[dict[str, Any]]:
    with closing_connection(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1 ASC").fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _signal_id_from_recommendation(recommendation_id: Any, recommendation_type: Any) -> str:
    suffix = f":{recommendation_type}"
    text = str(recommendation_id or "")
    return text[: -len(suffix)] if text.endswith(suffix) else text


def _bounded_price(value: Any) -> float:
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return 0.5
    return min(0.99, max(0.01, price))


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _max_drawdown(pnl_values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown
