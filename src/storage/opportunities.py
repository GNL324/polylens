from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.web.redaction import redact_mapping

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prop_arbitrage_scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                sport TEXT,
                event_id TEXT,
                bookmaker TEXT,
                region TEXT,
                markets TEXT,
                bankroll REAL,
                props_fetched INTEGER DEFAULT 0,
                matched_pairs INTEGER DEFAULT 0,
                rejected_pairs INTEGER DEFAULT 0,
                opportunities_found INTEGER DEFAULT 0,
                scan_duration REAL,
                finished_at TEXT,
                saved_opportunity_ids TEXT,
                duplicate_count INTEGER DEFAULT 0,
                error_message TEXT,
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prop_arbitrage_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovered_at TEXT NOT NULL,
                scan_run_id INTEGER,
                player TEXT,
                sport TEXT,
                prop_type TEXT,
                line REAL,
                over_book TEXT,
                over_odds INTEGER,
                under_book TEXT,
                under_odds INTEGER,
                implied_probability_sum REAL,
                guaranteed_roi REAL,
                guaranteed_profit_amount REAL,
                ranking_score REAL,
                stake_over REAL,
                stake_under REAL,
                opportunity_type TEXT,
                status TEXT DEFAULT 'open',
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER,
                executed_at TEXT,
                stake REAL,
                expected_profit REAL,
                status TEXT,
                notes TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                alert_type TEXT,
                channel TEXT,
                opportunity_id INTEGER,
                player TEXT,
                market_title TEXT,
                roi REAL,
                profit REAL,
                status TEXT,
                reason TEXT,
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scanner_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sport TEXT NOT NULL,
                markets TEXT NOT NULL,
                sportsbooks TEXT NOT NULL,
                bankroll REAL NOT NULL DEFAULT 1000,
                interval_seconds INTEGER NOT NULL DEFAULT 60,
                min_roi REAL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                template_id TEXT
            )
        """)
        _migrate_prop_arbitrage_scan_runs(conn)
        _migrate_prop_arbitrage_opportunities(conn)
        _migrate_paper_trades(conn)
        _migrate_scanner_profiles(conn)
        _ensure_default_scanner_profile(conn)


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


def save_prop_arbitrage_scan_result(
    result: dict[str, Any],
    *,
    db_path: str = DEFAULT_DB_PATH,
    sport: str | None = None,
    event_id: str | None = None,
    bookmaker: str | None = None,
    region: str = "us",
    markets: str | None = None,
    bankroll: float | None = None,
    discovered_at: str | None = None,
    finished_at: str | None = None,
    error_message: str | None = None,
    profile_id: int | None = None,
    profile_name: str | None = None,
    template_id: str | None = None,
) -> tuple[int, list[int]]:
    init_db(db_path)
    timestamp = discovered_at or datetime.now(timezone.utc).isoformat()
    finished = finished_at or datetime.now(timezone.utc).isoformat()
    opportunities = result.get("prop_arbitrage_candidates") or []
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO prop_arbitrage_scan_runs
            (started_at, sport, event_id, bookmaker, region, markets, bankroll, props_fetched, matched_pairs,
             rejected_pairs, opportunities_found, scan_duration, finished_at, saved_opportunity_ids, duplicate_count,
             error_message, raw_json, profile_id, profile_name, template_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                sport,
                event_id,
                bookmaker,
                region,
                markets,
                bankroll,
                int(result.get("props_fetched") or 0),
                int(result.get("matched_prop_pairs") or 0),
                int(result.get("rejected_pairs") or 0),
                len(opportunities),
                _num(result.get("scan_duration_seconds")),
                finished,
                "[]",
                0,
                error_message or result.get("error_message"),
                json.dumps(result, sort_keys=True),
                profile_id,
                profile_name,
                template_id,
            ),
        )
        scan_run_id = int(cur.lastrowid)

    ids: list[int] = []
    for opportunity in opportunities:
        opportunity_id = save_prop_arbitrage_opportunity(
            opportunity,
            db_path=db_path,
            sport=sport,
            scan_run_id=scan_run_id,
            discovered_at=timestamp,
            profile_id=profile_id,
            profile_name=profile_name,
            template_id=template_id,
        )
        if opportunity_id is not None:
            ids.append(opportunity_id)
    duplicate_count = max(0, len(opportunities) - len(ids))
    raw_result = dict(result)
    raw_result["saved_prop_opportunity_ids"] = ids
    raw_result["duplicate_count"] = duplicate_count
    if error_message or result.get("error_message"):
        raw_result["error_message"] = error_message or result.get("error_message")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE prop_arbitrage_scan_runs
            SET saved_opportunity_ids = ?, duplicate_count = ?, error_message = ?, raw_json = ?
            WHERE id = ?
            """,
            (json.dumps(ids, sort_keys=True), duplicate_count, error_message or result.get("error_message"), json.dumps(raw_result, sort_keys=True), scan_run_id),
        )
    return scan_run_id, ids


def create_scanner_profile(
    *,
    name: str,
    sport: str,
    markets: list[str] | str,
    sportsbooks: list[str] | str | None = None,
    bankroll: float = 1000.0,
    interval_seconds: int = 60,
    min_roi: float = 0.0,
    is_active: bool = False,
    template_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    markets_json = json.dumps(_list_value(markets), sort_keys=True)
    sportsbooks_json = json.dumps(_list_value(sportsbooks), sort_keys=True)
    with sqlite3.connect(db_path) as conn:
        if is_active:
            conn.execute("UPDATE scanner_profiles SET is_active = 0")
        cur = conn.execute(
            """
            INSERT INTO scanner_profiles
            (name, sport, markets, sportsbooks, bankroll, interval_seconds, min_roi, is_active, created_at, updated_at, template_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, sport, markets_json, sportsbooks_json, bankroll, interval_seconds, min_roi, 1 if is_active else 0, now, now, template_id),
        )
        return int(cur.lastrowid)


def update_scanner_profile(profile_id: int, *, db_path: str = DEFAULT_DB_PATH, **updates: Any) -> dict[str, Any] | None:
    init_db(db_path)
    allowed = {"name", "sport", "markets", "sportsbooks", "bankroll", "interval_seconds", "min_roi", "is_active", "template_id"}
    values: dict[str, Any] = {key: value for key, value in updates.items() if key in allowed and value is not None}
    if not values:
        return get_scanner_profile(profile_id, db_path=db_path)
    if "markets" in values:
        values["markets"] = json.dumps(_list_value(values["markets"]), sort_keys=True)
    if "sportsbooks" in values:
        values["sportsbooks"] = json.dumps(_list_value(values["sportsbooks"]), sort_keys=True)
    if "is_active" in values:
        values["is_active"] = 1 if bool(values["is_active"]) else 0
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        if values.get("is_active"):
            conn.execute("UPDATE scanner_profiles SET is_active = 0 WHERE id != ?", (profile_id,))
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(f"UPDATE scanner_profiles SET {assignments} WHERE id = ?", (*values.values(), profile_id))
    return get_scanner_profile(profile_id, db_path=db_path)


def delete_scanner_profile(profile_id: int, *, db_path: str = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM scanner_profiles WHERE id = ?", (profile_id,))
    if get_active_scanner_profile(db_path=db_path) is None:
        profiles = list_scanner_profiles(db_path=db_path)
        if profiles:
            set_active_scanner_profile(int(profiles[0]["id"]), db_path=db_path)


def set_active_scanner_profile(profile_id: int, *, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scanner_profiles SET is_active = 0")
        conn.execute(
            "UPDATE scanner_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), profile_id),
        )
    return get_scanner_profile(profile_id, db_path=db_path)


def get_scanner_profile(profile_id: int, *, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scanner_profiles WHERE id = ?", (profile_id,)).fetchone()
    return _profile_row(row) if row else None


def get_scanner_profile_by_name(name: str, *, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scanner_profiles WHERE name = ?", (name,)).fetchone()
    return _profile_row(row) if row else None


def get_active_scanner_profile(*, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scanner_profiles WHERE is_active = 1 ORDER BY id LIMIT 1").fetchone()
    return _profile_row(row) if row else None


def list_scanner_profiles(*, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM scanner_profiles ORDER BY is_active DESC, name").fetchall()
    return [_profile_row(row) for row in rows]


def resolve_scanner_profile(name: str | None, *, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    if not name:
        return None
    if name in {"__ACTIVE__", "ACTIVE", "active"}:
        return get_active_scanner_profile(db_path=db_path)
    return get_scanner_profile_by_name(name, db_path=db_path)


def unique_scanner_profile_name(base_name: str, *, db_path: str = DEFAULT_DB_PATH) -> str:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        existing = {row[0] for row in conn.execute("SELECT name FROM scanner_profiles").fetchall()}
    if base_name not in existing:
        return base_name
    candidate = f"{base_name} Copy"
    if candidate not in existing:
        return candidate
    index = 2
    while True:
        candidate = f"{base_name} Copy {index}"
        if candidate not in existing:
            return candidate
        index += 1


def create_scanner_profile_from_template(template: dict[str, Any], *, db_path: str = DEFAULT_DB_PATH, activate: bool = False) -> int:
    name = unique_scanner_profile_name(str(template["name"]), db_path=db_path)
    return create_scanner_profile(
        db_path=db_path,
        name=name,
        sport=str(template["sport"]),
        markets=list(template.get("markets") or []),
        sportsbooks=list(template.get("sportsbooks") or []),
        bankroll=float(template.get("bankroll") or 1000),
        interval_seconds=int(template.get("interval_seconds") or 60),
        min_roi=float(template.get("min_roi") or 0),
        is_active=activate,
        template_id=str(template.get("template_id") or "") or None,
    )


def duplicate_scanner_profile(profile_id: int, *, db_path: str = DEFAULT_DB_PATH) -> int | None:
    profile = get_scanner_profile(profile_id, db_path=db_path)
    if not profile:
        return None
    return create_scanner_profile(
        db_path=db_path,
        name=unique_scanner_profile_name(str(profile["name"]), db_path=db_path),
        sport=str(profile["sport"]),
        markets=list(profile.get("markets") or []),
        sportsbooks=list(profile.get("sportsbooks") or []),
        bankroll=float(profile.get("bankroll") or 1000),
        interval_seconds=int(profile.get("interval_seconds") or 60),
        min_roi=float(profile.get("min_roi") or 0),
        is_active=False,
        template_id=profile.get("template_id"),
    )


def save_prop_arbitrage_opportunity(
    opportunity: dict[str, Any],
    *,
    db_path: str = DEFAULT_DB_PATH,
    sport: str | None = None,
    scan_run_id: int | None = None,
    discovered_at: str | None = None,
    profile_id: int | None = None,
    profile_name: str | None = None,
    template_id: str | None = None,
) -> int | None:
    init_db(db_path)
    timestamp = discovered_at or datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.fromisoformat(timestamp.replace("Z", "+00:00")) - timedelta(hours=24)).isoformat()
    prop_type = opportunity.get("prop_type") or opportunity.get("market_type")
    with sqlite3.connect(db_path) as conn:
        duplicate = conn.execute(
            """
            SELECT id FROM prop_arbitrage_opportunities
            WHERE discovered_at >= ?
              AND COALESCE(player, '') = COALESCE(?, '')
              AND COALESCE(prop_type, '') = COALESCE(?, '')
              AND COALESCE(line, -999999) = COALESCE(?, -999999)
              AND COALESCE(over_book, '') = COALESCE(?, '')
              AND COALESCE(under_book, '') = COALESCE(?, '')
              AND COALESCE(over_odds, -999999) = COALESCE(?, -999999)
              AND COALESCE(under_odds, -999999) = COALESCE(?, -999999)
            ORDER BY id DESC LIMIT 1
            """,
            (
                cutoff,
                opportunity.get("player"),
                prop_type,
                _num(opportunity.get("line")),
                opportunity.get("over_book"),
                opportunity.get("under_book"),
                _int(opportunity.get("over_odds")),
                _int(opportunity.get("under_odds")),
            ),
        ).fetchone()
        if duplicate:
            return None
        cur = conn.execute(
            """
            INSERT INTO prop_arbitrage_opportunities
            (discovered_at, scan_run_id, player, sport, prop_type, line, over_book, over_odds, under_book,
             under_odds, implied_probability_sum, guaranteed_roi, guaranteed_profit_amount, ranking_score,
             stake_over, stake_under, opportunity_type, status, raw_json, profile_id, profile_name, template_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                scan_run_id,
                opportunity.get("player"),
                sport or opportunity.get("sport"),
                prop_type,
                _num(opportunity.get("line")),
                opportunity.get("over_book"),
                _int(opportunity.get("over_odds")),
                opportunity.get("under_book"),
                _int(opportunity.get("under_odds")),
                _num(opportunity.get("implied_probability_sum")),
                _num(opportunity.get("guaranteed_roi")),
                _num(opportunity.get("guaranteed_profit_amount") if opportunity.get("guaranteed_profit_amount") is not None else opportunity.get("guaranteed_profit")),
                _num(opportunity.get("ranking_score")),
                _num(opportunity.get("stake_over")),
                _num(opportunity.get("stake_under")),
                opportunity.get("opportunity_type"),
                opportunity.get("status") or "open",
                json.dumps(opportunity, sort_keys=True),
                profile_id,
                profile_name,
                template_id,
            ),
        )
        return int(cur.lastrowid)


def load_prop_arbitrage_opportunities(
    *,
    db_path: str = DEFAULT_DB_PATH,
    min_roi: float | None = None,
    sport: str | None = None,
    bookmaker: str | None = None,
    player: str | None = None,
    prop_type: str | None = None,
    status: str | None = None,
    lifecycle_status: str | None = None,
    include_archived: bool = False,
    min_profit: float | None = None,
    max_roi: float | None = None,
    discovered_from: str | None = None,
    discovered_to: str | None = None,
    sort_by: str = "ranking_score",
    limit: int = 100,
) -> list[dict[str, Any]]:
    init_db(db_path)
    where = []
    params: list[Any] = []
    if min_roi is not None:
        where.append("guaranteed_roi >= ?")
        params.append(float(min_roi))
    if max_roi is not None:
        where.append("guaranteed_roi <= ?")
        params.append(float(max_roi))
    if min_profit is not None:
        where.append("guaranteed_profit_amount >= ?")
        params.append(float(min_profit))
    if sport:
        where.append("sport = ?")
        params.append(sport)
    if bookmaker:
        where.append("(over_book = ? OR under_book = ?)")
        params.extend([bookmaker, bookmaker])
    if player:
        where.append("LOWER(player) LIKE ?")
        params.append(f"%{player.lower()}%")
    if prop_type:
        where.append("prop_type = ?")
        params.append(prop_type)
    if status:
        where.append("status = ?")
        params.append(status)
    if lifecycle_status:
        where.append("lifecycle_status = ?")
        params.append(lifecycle_status)
    elif not include_archived:
        where.append("COALESCE(lifecycle_status, 'discovered') != 'archived'")
    if discovered_from:
        where.append("discovered_at >= ?")
        params.append(discovered_from)
    if discovered_to:
        where.append("discovered_at <= ?")
        params.append(discovered_to)
    sql = "SELECT * FROM prop_arbitrage_opportunities"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order_by = {
        "roi": "guaranteed_roi DESC, ranking_score DESC, discovered_at DESC",
        "profit": "guaranteed_profit_amount DESC, ranking_score DESC, discovered_at DESC",
        "discovery_date": "discovered_at DESC, ranking_score DESC",
        "ranking_score": "ranking_score DESC, guaranteed_roi DESC, discovered_at DESC",
    }.get(sort_by, "ranking_score DESC, guaranteed_roi DESC, discovered_at DESC")
    sql += f" ORDER BY {order_by} LIMIT ?"
    params.append(max(1, int(limit)))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def get_prop_arbitrage_opportunity(opportunity_id: int, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM prop_arbitrage_opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["raw"] = _loads(data.get("raw_json"), {})
    return data


def mark_opportunity_viewed(opportunity_id: int, *, db_path: str = DEFAULT_DB_PATH, viewed_at: str | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    viewed_at = viewed_at or datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        _migrate_prop_arbitrage_opportunities(conn)
        conn.execute(
            """
            UPDATE prop_arbitrage_opportunities
            SET first_viewed_at = COALESCE(first_viewed_at, ?),
                lifecycle_status = CASE
                    WHEN lifecycle_status IN ('paper_traded', 'settled', 'archived') THEN lifecycle_status
                    ELSE 'viewed'
                END
            WHERE id = ?
            """,
            (viewed_at, opportunity_id),
        )
    return get_prop_arbitrage_opportunity(opportunity_id, db_path=db_path)


def archive_opportunity(opportunity_id: int, *, db_path: str = DEFAULT_DB_PATH, archived_at: str | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    archived_at = archived_at or datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        _migrate_prop_arbitrage_opportunities(conn)
        conn.execute(
            "UPDATE prop_arbitrage_opportunities SET lifecycle_status = 'archived', archived_at = ?, status = 'archived' WHERE id = ?",
            (archived_at, opportunity_id),
        )
    return get_prop_arbitrage_opportunity(opportunity_id, db_path=db_path)


def opportunity_timeline(opportunity_id: int, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    opportunity = get_prop_arbitrage_opportunity(opportunity_id, db_path=db_path)
    if not opportunity:
        return []
    trade: dict[str, Any] | None = None
    paper_trade_id = opportunity.get("paper_trade_id")
    if paper_trade_id:
        trade = get_paper_trade(int(paper_trade_id), db_path=db_path)
    return [
        {"event": "discovered", "timestamp": opportunity.get("discovered_at")},
        {"event": "viewed", "timestamp": opportunity.get("first_viewed_at")},
        {"event": "paper_traded", "timestamp": trade.get("executed_at") if trade else None},
        {"event": "settled", "timestamp": trade.get("settled_at") if trade else None},
        {"event": "archived", "timestamp": opportunity.get("archived_at")},
    ]


def create_paper_trade(
    opportunity_id: int,
    *,
    db_path: str = DEFAULT_DB_PATH,
    stake: float | None = None,
    expected_profit: float | None = None,
    result_status: str = "open",
    notes: str = "",
    executed_at: str | None = None,
) -> int:
    init_db(db_path)
    executed_at = executed_at or datetime.now(timezone.utc).isoformat()
    opportunity = get_prop_arbitrage_opportunity(opportunity_id, db_path=db_path)
    if not opportunity:
        raise ValueError(f"opportunity not found: {opportunity_id}")
    total_stake = _float0(opportunity.get("stake_over")) + _float0(opportunity.get("stake_under"))
    if stake is not None:
        total_stake = float(stake)
    expected_profit = _float0(opportunity.get("guaranteed_profit_amount")) if expected_profit is None else float(expected_profit)
    expected_roi = (expected_profit / total_stake) if total_stake else _float0(opportunity.get("guaranteed_roi"))
    raw = opportunity.get("raw") or _loads(opportunity.get("raw_json"), {})
    with sqlite3.connect(db_path) as conn:
        _migrate_paper_trades(conn)
        existing = conn.execute(
            "SELECT id FROM paper_trades WHERE opportunity_id = ? ORDER BY id DESC LIMIT 1",
            (opportunity_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE prop_arbitrage_opportunities
                SET lifecycle_status = CASE WHEN lifecycle_status = 'archived' THEN lifecycle_status ELSE 'paper_traded' END,
                    paper_trade_id = COALESCE(paper_trade_id, ?)
                WHERE id = ?
                """,
                (int(existing[0]), opportunity_id),
            )
            return int(existing[0])
        cur = conn.execute(
            """
            INSERT INTO paper_trades
            (opportunity_id, executed_at, player, prop_type, line, over_book, over_odds, stake_over,
             under_book, under_odds, stake_under, total_stake, expected_profit, expected_roi,
             result_status, realized_profit, settled_at, notes, raw_json, stake, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_id,
                executed_at,
                opportunity.get("player"),
                opportunity.get("prop_type"),
                _num(opportunity.get("line")),
                opportunity.get("over_book"),
                _int(opportunity.get("over_odds")),
                _num(opportunity.get("stake_over")),
                opportunity.get("under_book"),
                _int(opportunity.get("under_odds")),
                _num(opportunity.get("stake_under")),
                _num(total_stake),
                _num(expected_profit),
                _num(expected_roi),
                result_status,
                None,
                None,
                notes,
                json.dumps(raw, sort_keys=True),
                _num(total_stake),
                result_status,
            ),
        )
        conn.execute("UPDATE prop_arbitrage_opportunities SET status = 'paper' WHERE id = ?", (opportunity_id,))
        conn.execute(
            """
            UPDATE prop_arbitrage_opportunities
            SET lifecycle_status = 'paper_traded', paper_trade_id = ?
            WHERE id = ?
            """,
            (int(cur.lastrowid), opportunity_id),
        )
        return int(cur.lastrowid)


def load_paper_trades(*, db_path: str = DEFAULT_DB_PATH, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE result_status = ?"
        params.append(status)
    params.append(max(1, int(limit)))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_paper_trades(conn)
        rows = conn.execute(
            f"""
            SELECT id, opportunity_id, executed_at, player, prop_type, line, over_book, over_odds, stake_over,
                   under_book, under_odds, stake_under, total_stake, expected_profit, expected_roi,
                   result_status, realized_profit, settled_at, notes
            FROM paper_trades
            {where}
            ORDER BY COALESCE(executed_at, '') DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def settle_paper_trade(
    trade_id: int,
    outcome: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    realized_profit: float | None = None,
    notes: str | None = None,
    settled_at: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    outcome = outcome.strip().lower().replace("_", "-")
    if outcome not in {"over-won", "under-won", "push", "void"}:
        raise ValueError(f"unsupported settlement outcome: {outcome}")
    settled_at = settled_at or datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_paper_trades(conn)
        row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            raise ValueError(f"paper trade not found: {trade_id}")
        trade = dict(row)
        calculated = _settlement_profit(trade, outcome)
        final_profit = float(realized_profit) if realized_profit is not None else calculated
        status = "won" if final_profit > 0 else "lost" if final_profit < 0 else "push"
        if outcome == "void":
            status = "void"
        merged_notes = trade.get("notes") or ""
        if notes:
            merged_notes = f"{merged_notes}\n{notes}".strip()
        conn.execute(
            """
            UPDATE paper_trades
            SET result_status = ?, realized_profit = ?, settled_at = ?, notes = ?, status = ?
            WHERE id = ?
            """,
            (status, round(final_profit, 6), settled_at, merged_notes, status, trade_id),
        )
        if trade.get("opportunity_id"):
            conn.execute(
                "UPDATE prop_arbitrage_opportunities SET lifecycle_status = 'settled' WHERE id = ? AND lifecycle_status != 'archived'",
                (trade.get("opportunity_id"),),
            )
    updated = get_paper_trade(trade_id, db_path=db_path)
    if updated is None:
        raise ValueError(f"paper trade not found after settlement: {trade_id}")
    return updated


def get_paper_trade(trade_id: int, *, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_paper_trades(conn)
        row = conn.execute("SELECT * FROM paper_trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def paper_trade_summary(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_paper_trades(conn)
        rows = [dict(row) for row in conn.execute("SELECT * FROM paper_trades")]
    open_rows = [row for row in rows if (row.get("result_status") or "open") == "open"]
    settled_rows = [row for row in rows if (row.get("result_status") or "open") != "open"]
    realized = [_float0(row.get("realized_profit")) for row in settled_rows]
    expected_roi_values = [_float0(row.get("expected_roi")) for row in rows if row.get("expected_roi") is not None]
    wins = [value for value in realized if value > 0]
    return {
        "open_paper_trades": len(open_rows),
        "settled_paper_trades": len(settled_rows),
        "expected_open_profit": round(sum(_float0(row.get("expected_profit")) for row in open_rows), 6),
        "realized_paper_pnl": round(sum(realized), 6),
        "average_expected_roi": round(sum(expected_roi_values) / len(expected_roi_values), 6) if expected_roi_values else 0.0,
        "win_rate": round(len(wins) / len(settled_rows), 6) if settled_rows else 0.0,
        "best_trade": round(max(realized), 6) if realized else 0.0,
        "worst_trade": round(min(realized), 6) if realized else 0.0,
    }


def paper_pnl_analytics(db_path: str = DEFAULT_DB_PATH) -> dict[str, list[dict[str, Any]]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_paper_trades(conn)
        settled = [dict(row) for row in conn.execute("SELECT * FROM paper_trades WHERE result_status != 'open' ORDER BY COALESCE(settled_at, executed_at, '') ASC, id ASC")]
        all_rows = [dict(row) for row in conn.execute("SELECT * FROM paper_trades ORDER BY id ASC")]
    cumulative: list[dict[str, Any]] = []
    running = 0.0
    daily: dict[str, float] = {}
    for row in settled:
        pnl = _float0(row.get("realized_profit"))
        running += pnl
        date = str(row.get("settled_at") or row.get("executed_at") or "")[:10] or "unknown"
        daily[date] = daily.get(date, 0.0) + pnl
        cumulative.append({"timestamp": row.get("settled_at") or row.get("executed_at"), "cumulative_pnl": round(running, 6)})
    by_pair: dict[str, dict[str, float]] = {}
    for row in all_rows:
        pair = f"{row.get('over_book') or 'unknown'} / {row.get('under_book') or 'unknown'}"
        item = by_pair.setdefault(pair, {"expected_profit": 0.0, "realized_profit": 0.0, "total_stake": 0.0, "trades": 0})
        item["expected_profit"] += _float0(row.get("expected_profit"))
        item["realized_profit"] += _float0(row.get("realized_profit"))
        item["total_stake"] += _float0(row.get("total_stake"))
        item["trades"] += 1
    return {
        "cumulative_pnl": cumulative,
        "daily_pnl": [{"date": date, "realized_profit": round(value, 6)} for date, value in sorted(daily.items())],
        "expected_vs_realized": [
            {"metric": "Expected", "profit": round(sum(_float0(row.get("expected_profit")) for row in all_rows), 6)},
            {"metric": "Realized", "profit": round(sum(_float0(row.get("realized_profit")) for row in all_rows), 6)},
        ],
        "roi_by_bookmaker_pair": [
            {
                "books": pair,
                "trades": int(values["trades"]),
                "expected_roi": round(values["expected_profit"] / values["total_stake"], 6) if values["total_stake"] else 0.0,
                "realized_roi": round(values["realized_profit"] / values["total_stake"], 6) if values["total_stake"] else 0.0,
            }
            for pair, values in sorted(by_pair.items())
        ],
    }


def prop_arbitrage_summary_today(db_path: str = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with sqlite3.connect(db_path) as conn:
        open_count = conn.execute("SELECT COUNT(*) FROM prop_arbitrage_opportunities WHERE status = 'open'").fetchone()[0]
        row = conn.execute(
            """
            SELECT COUNT(*) AS found_today,
                   MAX(guaranteed_roi) AS highest_roi_today,
                   AVG(guaranteed_roi) AS average_roi_today,
                   SUM(guaranteed_profit_amount) AS potential_profit_today
            FROM prop_arbitrage_opportunities
            WHERE discovered_at >= ?
            """,
            (day_start,),
        ).fetchone()
    return {
        "open_opportunities": int(open_count or 0),
        "opportunities_found_today": int(row[0] or 0),
        "highest_roi_today": float(row[1] or 0.0),
        "average_roi_today": float(row[2] or 0.0),
        "potential_profit_today": float(row[3] or 0.0),
    }


def latest_prop_scan_status(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM prop_arbitrage_scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def prop_watch_health(db_path: str = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    latest = latest_prop_scan_status(db_path) or {}
    metrics = alert_metrics(db_path, now=now)
    with sqlite3.connect(db_path) as conn:
        duplicate_alerts = conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'duplicate'",
            (day_start,),
        ).fetchone()[0]
        failures = conn.execute(
            "SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'failed'",
            (day_start,),
        ).fetchone()[0]
    return {
        "last_scan_time": latest.get("started_at"),
        "last_scan_finished_at": latest.get("finished_at"),
        "last_scan_duration": latest.get("scan_duration") or 0,
        "opportunities_found_last_scan": latest.get("opportunities_found") or 0,
        "saved_opportunity_ids": _loads(latest.get("saved_opportunity_ids"), []),
        "duplicate_count_last_scan": latest.get("duplicate_count") or 0,
        "error_message": latest.get("error_message"),
        "alerts_sent_today": metrics["alerts_sent_today"],
        "duplicates_skipped_today": int(duplicate_alerts or 0),
        "failures_today": int(failures or 0),
    }


def record_alert_event(
    *,
    db_path: str = DEFAULT_DB_PATH,
    alert_type: str | None = None,
    channel: str | None = None,
    opportunity_id: int | None = None,
    player: str | None = None,
    market_title: str | None = None,
    roi: float | None = None,
    profit: float | None = None,
    status: str = "skipped",
    reason: str | None = None,
    raw: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    init_db(db_path)
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    clean_raw = redact_mapping(raw or {})
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO alert_events
            (created_at, alert_type, channel, opportunity_id, player, market_title, roi, profit, status, reason, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                alert_type,
                channel,
                opportunity_id,
                player,
                market_title,
                _num(roi),
                _num(profit),
                status,
                reason,
                json.dumps(clean_raw, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)


def load_alert_events(*, db_path: str = DEFAULT_DB_PATH, limit: int = 100) -> list[dict[str, Any]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, created_at, alert_type, channel, opportunity_id, player, market_title,
                   roi, profit, status, reason, raw_json
            FROM alert_events
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def alert_metrics(db_path: str = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sent = conn.execute("SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'sent'", (day_start,)).fetchone()[0]
        duplicates = conn.execute("SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'duplicate'", (day_start,)).fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'failed'", (day_start,)).fetchone()[0]
        last = conn.execute("SELECT created_at FROM alert_events ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        avg_roi = conn.execute("SELECT AVG(roi) FROM alert_events WHERE created_at >= ? AND roi IS NOT NULL", (day_start,)).fetchone()[0]
        top = conn.execute(
            """
            SELECT player, market_title, roi, profit
            FROM alert_events
            WHERE created_at >= ?
            ORDER BY COALESCE(profit, 0) DESC, COALESCE(roi, 0) DESC
            LIMIT 1
            """,
            (day_start,),
        ).fetchone()
    return {
        "alerts_sent_today": int(sent or 0),
        "duplicates_suppressed_today": int(duplicates or 0),
        "failed_alerts_today": int(failed or 0),
        "last_alert_time": last["created_at"] if last else None,
        "top_alerting_opportunity_today": dict(top) if top else None,
        "average_alert_roi_today": float(avg_roi or 0.0),
    }


def opportunity_funnel(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    states = ["discovered", "viewed", "paper_traded", "settled", "archived"]
    with sqlite3.connect(db_path) as conn:
        _migrate_prop_arbitrage_opportunities(conn)
        counts = {
            row[0] or "discovered": int(row[1] or 0)
            for row in conn.execute(
                "SELECT COALESCE(lifecycle_status, 'discovered') AS state, COUNT(*) FROM prop_arbitrage_opportunities GROUP BY state"
            )
        }
    total = sum(counts.values())
    return [
        {"lifecycle_status": state, "count": counts.get(state, 0), "conversion_percent": round((counts.get(state, 0) / total), 6) if total else 0.0}
        for state in states
    ]


def operations_metrics(db_path: str = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    path = Path(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_prop_arbitrage_opportunities(conn)
        _migrate_paper_trades(conn)
        last_scan = conn.execute("SELECT * FROM prop_arbitrage_scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        scans_today = conn.execute("SELECT COUNT(*) FROM prop_arbitrage_scan_runs WHERE started_at >= ?", (day_start,)).fetchone()[0]
        opportunities_today = conn.execute("SELECT COUNT(*) FROM prop_arbitrage_opportunities WHERE discovered_at >= ?", (day_start,)).fetchone()[0]
        paper_trades_today = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE executed_at >= ?", (day_start,)).fetchone()[0]
        settled_trades_today = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE settled_at >= ?", (day_start,)).fetchone()[0]
        alerts_today = conn.execute("SELECT COUNT(*) FROM alerts WHERE timestamp >= ?", (day_start,)).fetchone()[0]
        duplicate_alerts_today = conn.execute("SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'duplicate'", (day_start,)).fetchone()[0]
        failed_alerts_today = conn.execute("SELECT COUNT(*) FROM alert_events WHERE created_at >= ? AND status = 'failed'", (day_start,)).fetchone()[0]
        last_alert = conn.execute("SELECT created_at FROM alert_events WHERE status = 'sent' ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        last_successful_scan = conn.execute("SELECT started_at FROM prop_arbitrage_scan_runs WHERE opportunities_found >= 0 ORDER BY id DESC LIMIT 1").fetchone()
        last_failed_scan = conn.execute("SELECT started_at FROM prop_arbitrage_scan_runs WHERE raw_json LIKE '%error%' ORDER BY id DESC LIMIT 1").fetchone()
    raw_scan = _loads(last_scan["raw_json"], {}) if last_scan else {}
    return {
        "last_scan_time": last_scan["started_at"] if last_scan else None,
        "scan_duration": last_scan["scan_duration"] if last_scan else 0.0,
        "scans_today": int(scans_today or 0),
        "opportunities_today": int(opportunities_today or 0),
        "paper_trades_today": int(paper_trades_today or 0),
        "settled_trades_today": int(settled_trades_today or 0),
        "alerts_today": int(alerts_today or 0),
        "duplicate_alerts_suppressed_today": int(duplicate_alerts_today or 0),
        "failed_alerts_today": int(failed_alerts_today or 0),
        "last_alert_sent": last_alert["created_at"] if last_alert else None,
        "last_successful_scan": last_successful_scan["started_at"] if last_successful_scan else None,
        "last_failed_scan": last_failed_scan["started_at"] if last_failed_scan else None,
        "database_size": path.stat().st_size if path.exists() else 0,
        "latest_scan": dict(last_scan) if last_scan else None,
        "rejection_reasons": raw_scan.get("rejection_reasons") or {},
    }


def bookmaker_pair_performance(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_prop_arbitrage_opportunities(conn)
        _migrate_paper_trades(conn)
        opportunities = [dict(row) for row in conn.execute("SELECT * FROM prop_arbitrage_opportunities")]
        trades = [dict(row) for row in conn.execute("SELECT * FROM paper_trades WHERE result_status != 'open'")]
    pairs: dict[str, dict[str, float]] = {}
    for row in opportunities:
        pair = _book_pair(row)
        item = pairs.setdefault(pair, {"opportunities_found": 0.0, "roi_total": 0.0, "profit_total": 0.0, "settled_trade_count": 0.0, "realized_pnl": 0.0})
        item["opportunities_found"] += 1
        item["roi_total"] += _float0(row.get("guaranteed_roi"))
        item["profit_total"] += _float0(row.get("guaranteed_profit_amount"))
    for row in trades:
        pair = _book_pair(row)
        item = pairs.setdefault(pair, {"opportunities_found": 0.0, "roi_total": 0.0, "profit_total": 0.0, "settled_trade_count": 0.0, "realized_pnl": 0.0})
        item["settled_trade_count"] += 1
        item["realized_pnl"] += _float0(row.get("realized_profit"))
    results = []
    for pair, values in sorted(pairs.items()):
        count = values["opportunities_found"]
        results.append(
            {
                "book_pair": pair,
                "opportunities_found": int(count),
                "average_roi": round(values["roi_total"] / count, 6) if count else 0.0,
                "average_profit": round(values["profit_total"] / count, 6) if count else 0.0,
                "settled_trade_count": int(values["settled_trade_count"]),
                "realized_pnl": round(values["realized_pnl"], 6),
            }
        )
    return results


def profile_performance(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        profiles = [_profile_row(row) for row in conn.execute("SELECT * FROM scanner_profiles ORDER BY name").fetchall()]
        scan_rows = [dict(row) for row in conn.execute("SELECT * FROM prop_arbitrage_scan_runs").fetchall()]
        opportunity_rows = [dict(row) for row in conn.execute("SELECT * FROM prop_arbitrage_opportunities").fetchall()]
        trade_rows = [dict(row) for row in conn.execute(
            """
            SELECT pt.*, o.profile_id, o.profile_name, o.template_id, o.over_book, o.under_book
            FROM paper_trades pt
            LEFT JOIN prop_arbitrage_opportunities o ON o.id = pt.opportunity_id
            """
        ).fetchall()]
        alert_rows = [dict(row) for row in conn.execute(
            """
            SELECT ae.*, o.profile_id, o.profile_name, o.template_id
            FROM alert_events ae
            LEFT JOIN prop_arbitrage_opportunities o ON o.id = ae.opportunity_id
            """
        ).fetchall()]

    by_key: dict[tuple[Any, str], dict[str, Any]] = {}
    for profile in profiles:
        by_key[_profile_key(profile)] = _empty_profile_performance(profile)

    for row in scan_rows:
        item = _performance_item(by_key, row)
        if not item:
            continue
        item["scan_count"] += 1
        started_at = row.get("started_at")
        if started_at and (not item["last_scan_time"] or str(started_at) > str(item["last_scan_time"])):
            item["last_scan_time"] = started_at

    for row in opportunity_rows:
        item = _performance_item(by_key, row)
        if not item:
            continue
        item["opportunities_found"] += 1
        if row.get("guaranteed_roi") is not None:
            item["_roi_values"].append(_float0(row.get("guaranteed_roi")))
        if row.get("guaranteed_profit_amount") is not None:
            profit = _float0(row.get("guaranteed_profit_amount"))
            item["_expected_profit_values"].append(profit)
            item["total_expected_profit"] += profit

    for row in trade_rows:
        item = _performance_item(by_key, row)
        if not item:
            continue
        item["paper_trades_created"] += 1
        if (row.get("result_status") or "open") != "open":
            item["settled_paper_trades"] += 1
        item["realized_pnl"] += _float0(row.get("realized_profit"))

    for row in alert_rows:
        item = _performance_item(by_key, row)
        if not item:
            continue
        item["alert_count"] += 1
        if row.get("status") == "duplicate":
            item["duplicate_alert_count"] += 1
        if row.get("status") == "failed":
            item["failed_alert_count"] += 1

    result = []
    for item in by_key.values():
        roi_values = item.pop("_roi_values")
        profit_values = item.pop("_expected_profit_values")
        item["average_roi"] = round(sum(roi_values) / len(roi_values), 6) if roi_values else 0.0
        item["max_roi"] = round(max(roi_values), 6) if roi_values else 0.0
        item["average_expected_profit"] = round(sum(profit_values) / len(profit_values), 6) if profit_values else 0.0
        item["total_expected_profit"] = round(item["total_expected_profit"], 6)
        item["realized_pnl"] = round(item["realized_pnl"], 6)
        result.append(item)
    return sorted(result, key=lambda row: (row["realized_pnl"], row["average_roi"], row["scan_count"]), reverse=True)


def template_performance(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in profile_performance(db_path):
        template_id = row.get("template_id")
        if not template_id:
            continue
        item = grouped.setdefault(
            str(template_id),
            {
                "template_id": template_id,
                "template_name": row.get("template_name") or _template_name(template_id),
                "profiles_created": 0,
                "scans": 0,
                "opportunities": 0,
                "_roi_total": 0.0,
                "_roi_count": 0,
                "paper_trades": 0,
                "realized_pnl": 0.0,
            },
        )
        item["profiles_created"] += 1
        item["scans"] += int(row.get("scan_count") or 0)
        opportunities = int(row.get("opportunities_found") or 0)
        item["opportunities"] += opportunities
        item["_roi_total"] += _float0(row.get("average_roi")) * opportunities
        item["_roi_count"] += opportunities
        item["paper_trades"] += int(row.get("paper_trades_created") or 0)
        item["realized_pnl"] += _float0(row.get("realized_pnl"))
    result = []
    for item in grouped.values():
        count = item.pop("_roi_count")
        roi_total = item.pop("_roi_total")
        item["average_roi"] = round(roi_total / count, 6) if count else 0.0
        item["realized_pnl"] = round(item["realized_pnl"], 6)
        result.append(item)
    return sorted(result, key=lambda row: (row["realized_pnl"], row["average_roi"], row["scans"]), reverse=True)


def book_pair_by_profile_performance(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    profiles = {_profile_key(profile): profile for profile in list_scanner_profiles(db_path=db_path)}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        opps = [dict(row) for row in conn.execute("SELECT * FROM prop_arbitrage_opportunities WHERE profile_id IS NOT NULL OR profile_name IS NOT NULL").fetchall()]
        trades = [dict(row) for row in conn.execute(
            """
            SELECT pt.*, o.profile_id, o.profile_name, o.over_book, o.under_book
            FROM paper_trades pt
            JOIN prop_arbitrage_opportunities o ON o.id = pt.opportunity_id
            WHERE o.profile_id IS NOT NULL OR o.profile_name IS NOT NULL
            """
        ).fetchall()]
    grouped: dict[tuple[Any, str, str], dict[str, Any]] = {}
    for row in opps:
        profile = profiles.get(_profile_key(row), {})
        profile_name = row.get("profile_name") or profile.get("name") or ""
        key = (_profile_key(row), profile_name, _book_pair(row))
        item = grouped.setdefault(key, {"profile": profile_name, "book_pair": key[2], "opportunities": 0, "_roi_values": [], "paper_trades": 0, "realized_pnl": 0.0})
        item["opportunities"] += 1
        if row.get("guaranteed_roi") is not None:
            item["_roi_values"].append(_float0(row.get("guaranteed_roi")))
    for row in trades:
        profile = profiles.get(_profile_key(row), {})
        profile_name = row.get("profile_name") or profile.get("name") or ""
        key = (_profile_key(row), profile_name, _book_pair(row))
        item = grouped.setdefault(key, {"profile": profile_name, "book_pair": key[2], "opportunities": 0, "_roi_values": [], "paper_trades": 0, "realized_pnl": 0.0})
        item["paper_trades"] += 1
        item["realized_pnl"] += _float0(row.get("realized_profit"))
    result = []
    for item in grouped.values():
        roi_values = item.pop("_roi_values")
        item["average_roi"] = round(sum(roi_values) / len(roi_values), 6) if roi_values else 0.0
        item["realized_pnl"] = round(item["realized_pnl"], 6)
        result.append(item)
    return sorted(result, key=lambda row: (row["profile"], row["book_pair"]))


def active_profile_operations_metrics(db_path: str = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    init_db(db_path)
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    active = get_active_scanner_profile(db_path=db_path)
    if not active:
        return {"active_profile": None, "scans_today": 0, "opportunities_today": 0, "alerts_today": 0, "summary": None}
    profile_id = active.get("id")
    profile_name = active.get("name")
    summary = next((row for row in profile_performance(db_path) if row.get("profile_id") == profile_id), None)
    with sqlite3.connect(db_path) as conn:
        scans = conn.execute("SELECT COUNT(*) FROM prop_arbitrage_scan_runs WHERE started_at >= ? AND (profile_id = ? OR profile_name = ?)", (day_start, profile_id, profile_name)).fetchone()[0]
        opportunities = conn.execute("SELECT COUNT(*) FROM prop_arbitrage_opportunities WHERE discovered_at >= ? AND (profile_id = ? OR profile_name = ?)", (day_start, profile_id, profile_name)).fetchone()[0]
        alerts = conn.execute(
            """
            SELECT COUNT(*)
            FROM alert_events ae
            JOIN prop_arbitrage_opportunities o ON o.id = ae.opportunity_id
            WHERE ae.created_at >= ? AND (o.profile_id = ? OR o.profile_name = ?)
            """,
            (day_start, profile_id, profile_name),
        ).fetchone()[0]
    return {"active_profile": active, "scans_today": int(scans or 0), "opportunities_today": int(opportunities or 0), "alerts_today": int(alerts or 0), "summary": summary}


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


def _profile_key(row: dict[str, Any]) -> tuple[Any, str]:
    profile_id = row.get("profile_id") if row.get("profile_id") is not None else row.get("id")
    profile_name = str(row.get("profile_name") or row.get("name") or "")
    return profile_id, profile_name


def _empty_profile_performance(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_id": profile.get("id"),
        "profile": profile.get("name"),
        "profile_name": profile.get("name"),
        "template_id": profile.get("template_id"),
        "template_name": _template_name(profile.get("template_id")),
        "sport": profile.get("sport"),
        "markets": profile.get("markets") or [],
        "sportsbooks": profile.get("sportsbooks") or [],
        "scan_count": 0,
        "last_scan_time": None,
        "opportunities_found": 0,
        "_roi_values": [],
        "_expected_profit_values": [],
        "average_roi": 0.0,
        "max_roi": 0.0,
        "average_expected_profit": 0.0,
        "total_expected_profit": 0.0,
        "paper_trades_created": 0,
        "settled_paper_trades": 0,
        "realized_pnl": 0.0,
        "alert_count": 0,
        "duplicate_alert_count": 0,
        "failed_alert_count": 0,
    }


def _performance_item(items: dict[tuple[Any, str], dict[str, Any]], row: dict[str, Any]) -> dict[str, Any] | None:
    key = _profile_key(row)
    item = items.get(key)
    if item or not (key[0] or key[1]):
        return item
    item = _empty_profile_performance(
        {
            "id": key[0],
            "name": key[1],
            "template_id": row.get("template_id"),
            "sport": row.get("sport"),
            "markets": [],
            "sportsbooks": [],
        }
    )
    items[key] = item
    return item


def _template_name(template_id: Any) -> str:
    if not template_id:
        return ""
    try:
        from src.web.profile_templates import get_scanner_profile_template

        template = get_scanner_profile_template(str(template_id))
        return str(template["name"]) if template else ""
    except Exception:
        return ""


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float0(value: Any) -> float:
    number = _num(value)
    return float(number or 0.0)


def _migrate_paper_trades(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
    migrations = {
        "player": "TEXT",
        "prop_type": "TEXT",
        "line": "REAL",
        "over_book": "TEXT",
        "over_odds": "INTEGER",
        "stake_over": "REAL",
        "under_book": "TEXT",
        "under_odds": "INTEGER",
        "stake_under": "REAL",
        "total_stake": "REAL",
        "expected_roi": "REAL",
        "result_status": "TEXT DEFAULT 'open'",
        "realized_profit": "REAL",
        "settled_at": "TEXT",
        "raw_json": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {column} {definition}")
    conn.execute("UPDATE paper_trades SET result_status = COALESCE(result_status, 'open')")
    conn.execute("UPDATE paper_trades SET result_status = status WHERE status IS NOT NULL AND status != '' AND result_status = 'open' AND status != 'open'")
    conn.execute("UPDATE paper_trades SET total_stake = COALESCE(total_stake, stake, 0)")


def _migrate_prop_arbitrage_scan_runs(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prop_arbitrage_scan_runs)").fetchall()}
    migrations = {
        "finished_at": "TEXT",
        "saved_opportunity_ids": "TEXT",
        "duplicate_count": "INTEGER DEFAULT 0",
        "error_message": "TEXT",
        "profile_id": "INTEGER",
        "profile_name": "TEXT",
        "template_id": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE prop_arbitrage_scan_runs ADD COLUMN {column} {definition}")
    conn.execute("UPDATE prop_arbitrage_scan_runs SET duplicate_count = COALESCE(duplicate_count, 0)")
    conn.execute("UPDATE prop_arbitrage_scan_runs SET saved_opportunity_ids = COALESCE(saved_opportunity_ids, '[]')")


def _ensure_default_scanner_profile(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    count = conn.execute("SELECT COUNT(*) FROM scanner_profiles").fetchone()[0]
    if count:
        return
    conn.execute(
        """
        INSERT INTO scanner_profiles
        (name, sport, markets, sportsbooks, bankroll, interval_seconds, min_roi, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("NBA Player Points", "basketball_nba", json.dumps(["player_points"]), json.dumps([]), 1000.0, 60, 0.0, 1, now, now),
    )


def _migrate_scanner_profiles(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scanner_profiles)").fetchall()}
    migrations = {
        "template_id": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE scanner_profiles ADD COLUMN {column} {definition}")


def _migrate_prop_arbitrage_opportunities(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(prop_arbitrage_opportunities)").fetchall()}
    migrations = {
        "lifecycle_status": "TEXT DEFAULT 'discovered'",
        "first_viewed_at": "TEXT",
        "paper_trade_id": "INTEGER",
        "archived_at": "TEXT",
        "profile_id": "INTEGER",
        "profile_name": "TEXT",
        "template_id": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE prop_arbitrage_opportunities ADD COLUMN {column} {definition}")
    conn.execute("UPDATE prop_arbitrage_opportunities SET lifecycle_status = COALESCE(lifecycle_status, 'discovered')")


def _book_pair(row: dict[str, Any]) -> str:
    return f"{row.get('over_book') or 'unknown'} <-> {row.get('under_book') or 'unknown'}"


def _settlement_profit(trade: dict[str, Any], outcome: str) -> float:
    if outcome in {"push", "void"}:
        return 0.0
    total_stake = _float0(trade.get("total_stake"))
    if outcome == "over-won":
        payout_profit = _american_profit(_float0(trade.get("stake_over")), _int(trade.get("over_odds")) or 0)
    else:
        payout_profit = _american_profit(_float0(trade.get("stake_under")), _int(trade.get("under_odds")) or 0)
    return round(payout_profit - (total_stake - (_float0(trade.get("stake_over")) if outcome == "over-won" else _float0(trade.get("stake_under")))), 6)


def _american_profit(stake: float, odds: int) -> float:
    if stake <= 0 or odds == 0:
        return 0.0
    if odds > 0:
        return stake * (odds / 100)
    return stake * (100 / abs(odds))


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _list_value(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = _loads(text, [])
            items = parsed if isinstance(parsed, list) else []
        else:
            items = text.split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
    profile = dict(row)
    profile["markets"] = _list_value(profile.get("markets"))
    profile["sportsbooks"] = _list_value(profile.get("sportsbooks"))
    profile["is_active"] = bool(profile.get("is_active"))
    return profile
