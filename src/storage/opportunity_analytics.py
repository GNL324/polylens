from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

DEFAULT_DB_PATH = "data/opportunities.db"


def init_opportunity_analytics_db(db_path: str = DEFAULT_DB_PATH) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                scan_run_id INTEGER,
                opportunity_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                provider TEXT,
                sportsbook_pair TEXT,
                over_book TEXT,
                under_book TEXT,
                player TEXT,
                prop_identity TEXT,
                prop_period TEXT,
                line REAL,
                over_odds REAL,
                under_odds REAL,
                implied_probability_sum REAL,
                roi REAL,
                rejection_reason TEXT,
                raw_json TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_snapshots_observed_at ON opportunity_snapshots(observed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_snapshots_opportunity_id ON opportunity_snapshots(opportunity_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_snapshots_stage ON opportunity_snapshots(stage)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_lifecycle (
                opportunity_id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                max_roi REAL,
                min_roi REAL,
                latest_roi REAL,
                provider TEXT,
                sportsbook_pair TEXT,
                over_book TEXT,
                under_book TEXT,
                player TEXT,
                prop_identity TEXT,
                prop_period TEXT,
                line REAL,
                status TEXT NOT NULL DEFAULT 'active',
                inactive_at TEXT,
                raw_json TEXT
            )
            """
        )


def stable_opportunity_id(row: dict[str, Any]) -> str:
    parts = [
        row.get("provider"),
        row.get("over_book"),
        row.get("under_book"),
        row.get("player"),
        row.get("prop_identity") or row.get("prop_type") or row.get("market_type"),
        row.get("prop_period") or "full_game",
        _norm_line(row.get("line")),
    ]
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_scan_analytics(
    result: dict[str, Any],
    *,
    db_path: str = DEFAULT_DB_PATH,
    scan_run_id: int | None = None,
    observed_at: str | None = None,
    inactive_after_seconds: int | None = None,
) -> dict[str, Any]:
    init_opportunity_analytics_db(db_path)
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    rows = _snapshot_rows(result, timestamp, scan_run_id)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO opportunity_snapshots
            (observed_at, scan_run_id, opportunity_id, stage, provider, sportsbook_pair, over_book, under_book,
             player, prop_identity, prop_period, line, over_odds, under_odds, implied_probability_sum, roi,
             rejection_reason, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["observed_at"],
                    row.get("scan_run_id"),
                    row["opportunity_id"],
                    row["stage"],
                    row.get("provider"),
                    row.get("sportsbook_pair"),
                    row.get("over_book"),
                    row.get("under_book"),
                    row.get("player"),
                    row.get("prop_identity"),
                    row.get("prop_period"),
                    _num(row.get("line")),
                    _num(row.get("over_odds")),
                    _num(row.get("under_odds")),
                    _num(row.get("implied_probability_sum")),
                    _num(row.get("roi")),
                    row.get("rejection_reason"),
                    json.dumps(row.get("raw") or {}, sort_keys=True),
                )
                for row in rows
            ],
        )
        for row in rows:
            if row["stage"] != "final":
                continue
            _upsert_lifecycle(conn, row)
        if inactive_after_seconds is not None:
            mark_inactive(conn, timestamp, inactive_after_seconds)
    return {"snapshots_recorded": len(rows), "final_observed": sum(1 for row in rows if row["stage"] == "final")}


def mark_inactive(conn_or_path: sqlite3.Connection | str = DEFAULT_DB_PATH, observed_at: str | None = None, inactive_after_seconds: int = 900) -> int:
    timestamp = _parse_time(observed_at) or datetime.now(timezone.utc)
    cutoff = (timestamp - timedelta(seconds=inactive_after_seconds)).isoformat()
    if isinstance(conn_or_path, sqlite3.Connection):
        cur = conn_or_path.execute(
            "UPDATE opportunity_lifecycle SET status = 'inactive', inactive_at = ? WHERE status = 'active' AND last_seen < ?",
            (timestamp.isoformat(), cutoff),
        )
        return int(cur.rowcount or 0)
    init_opportunity_analytics_db(conn_or_path)
    with sqlite3.connect(conn_or_path) as conn:
        return mark_inactive(conn, timestamp.isoformat(), inactive_after_seconds)


def opportunity_leaderboard(db_path: str = DEFAULT_DB_PATH, limit: int = 20) -> dict[str, Any]:
    init_opportunity_analytics_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _backfill_historical_if_needed(conn)
        final_rows = [dict(row) for row in conn.execute("SELECT * FROM opportunity_snapshots WHERE stage = 'final'")]
        lifecycle_rows = [dict(row) for row in conn.execute("SELECT * FROM opportunity_lifecycle")]
    lifetime_by_id = {row["opportunity_id"]: _duration_seconds(row.get("first_seen"), row.get("last_seen")) for row in lifecycle_rows}
    return {
        "sportsbook_pairs": _rank(final_rows, "sportsbook_pair", lifetime_by_id, limit),
        "providers": _rank(final_rows, "provider", lifetime_by_id, limit),
        "prop_types": _rank(final_rows, "prop_identity", lifetime_by_id, limit),
        "players": _rank(final_rows, "player", lifetime_by_id, limit),
    }


def opportunity_lifetimes(db_path: str = DEFAULT_DB_PATH, limit: int = 20) -> dict[str, Any]:
    init_opportunity_analytics_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _backfill_historical_if_needed(conn)
        rows = [dict(row) for row in conn.execute("SELECT * FROM opportunity_lifecycle")]
    durations = [_duration_seconds(row.get("first_seen"), row.get("last_seen")) for row in rows]
    buckets = {"<5m": 0, "5-15m": 0, "15-60m": 0, "1-6h": 0, ">6h": 0}
    for duration in durations:
        if duration < 300:
            buckets["<5m"] += 1
        elif duration < 900:
            buckets["5-15m"] += 1
        elif duration < 3600:
            buckets["15-60m"] += 1
        elif duration < 21600:
            buckets["1-6h"] += 1
        else:
            buckets[">6h"] += 1
    longest = sorted(
        [{**row, "duration_seconds": _duration_seconds(row.get("first_seen"), row.get("last_seen"))} for row in rows],
        key=lambda row: row["duration_seconds"],
        reverse=True,
    )[:limit]
    return {
        "survival_histogram": buckets,
        "average_duration_seconds": round(sum(durations) / len(durations), 3) if durations else 0,
        "longest_lived": longest,
    }


def opportunity_quality_report(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_opportunity_analytics_db(db_path)
    with sqlite3.connect(db_path) as conn:
        _backfill_historical_if_needed(conn)
        rows = conn.execute("SELECT rejection_reason, COUNT(*) FROM opportunity_snapshots WHERE stage = 'rejected' GROUP BY rejection_reason").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM opportunity_snapshots WHERE stage = 'rejected'").fetchone()[0] or 0
    counts = {str(reason or "unknown"): int(count or 0) for reason, count in rows}
    def pct(reason: str) -> float:
        return round((counts.get(reason, 0) / total) * 100, 4) if total else 0.0
    return {
        "total_rejections": total,
        "rejection_counts": counts,
        "stale_leg_rejection_percent": pct("stale leg"),
        "period_mismatch_percent": pct("period mismatch"),
        "market_mismatch_percent": pct("market mismatch"),
        "invalid_side_percent": pct("invalid side"),
    }


def opportunity_analytics_dashboard(db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_opportunity_analytics_db(db_path)
    leaderboard = opportunity_leaderboard(db_path=db_path, limit=10)
    lifetimes = opportunity_lifetimes(db_path=db_path, limit=10)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _backfill_historical_if_needed(conn)
        per_day = [dict(row) for row in conn.execute(
            "SELECT substr(observed_at, 1, 10) AS day, COUNT(*) AS opportunities FROM opportunity_snapshots WHERE stage = 'final' GROUP BY day ORDER BY day"
        )]
        roi_rows = [row[0] for row in conn.execute("SELECT roi FROM opportunity_snapshots WHERE stage = 'final' AND roi IS NOT NULL")]
        highest = [dict(row) for row in conn.execute(
            "SELECT observed_at, player, prop_identity, prop_period, sportsbook_pair, roi FROM opportunity_snapshots WHERE stage = 'final' ORDER BY roi DESC LIMIT 10"
        )]
    return {
        "leaderboard": leaderboard,
        "lifetimes": lifetimes,
        "highest_roi": highest,
        "opportunities_per_day": per_day,
        "roi_distribution": _histogram([float(value) for value in roi_rows], [0, 0.01, 0.02, 0.05, 0.1]),
        "lifetime_distribution": lifetimes["survival_histogram"],
    }


def _snapshot_rows(result: dict[str, Any], observed_at: str, scan_run_id: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stage, items in (
        ("pre_filter", result.get("pre_filter_candidates") or []),
        ("final", result.get("prop_arbitrage_candidates") or []),
        ("rejected", result.get("analytics_rejected_candidates") or result.get("diagnostics") or []),
    ):
        for item in items:
            row = _snapshot_row(item, stage, observed_at, scan_run_id)
            if row:
                rows.append(row)
    return rows


def _backfill_historical_if_needed(conn: sqlite3.Connection) -> None:
    snapshot_count = conn.execute("SELECT COUNT(*) FROM opportunity_snapshots").fetchone()[0] or 0
    if snapshot_count:
        return
    table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prop_arbitrage_opportunities'").fetchone()
    if not table:
        return
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("SELECT * FROM prop_arbitrage_opportunities ORDER BY discovered_at, id")]
    for source in rows:
        raw = _json_obj(source.get("raw_json"))
        item = {
            "player": source.get("player"),
            "prop_type": source.get("prop_type"),
            "prop_identity": raw.get("prop_identity") or source.get("prop_type"),
            "prop_period": raw.get("prop_period") or "full_game",
            "line": source.get("line"),
            "over_book": source.get("over_book"),
            "under_book": source.get("under_book"),
            "over_odds": source.get("over_odds"),
            "under_odds": source.get("under_odds"),
            "implied_probability_sum": source.get("implied_probability_sum"),
            "guaranteed_roi": source.get("guaranteed_roi"),
            "over_source": raw.get("over_source"),
            "under_source": raw.get("under_source"),
        }
        observed_at = source.get("discovered_at") or datetime.now(timezone.utc).isoformat()
        row = _snapshot_row(item, "final", observed_at, source.get("scan_run_id"))
        if not row:
            continue
        conn.execute(
            """
            INSERT INTO opportunity_snapshots
            (observed_at, scan_run_id, opportunity_id, stage, provider, sportsbook_pair, over_book, under_book,
             player, prop_identity, prop_period, line, over_odds, under_odds, implied_probability_sum, roi,
             rejection_reason, raw_json)
            VALUES (?, ?, ?, 'final', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                row["observed_at"],
                row.get("scan_run_id"),
                row["opportunity_id"],
                row.get("provider"),
                row.get("sportsbook_pair"),
                row.get("over_book"),
                row.get("under_book"),
                row.get("player"),
                row.get("prop_identity"),
                row.get("prop_period"),
                _num(row.get("line")),
                _num(row.get("over_odds")),
                _num(row.get("under_odds")),
                _num(row.get("implied_probability_sum")),
                _num(row.get("roi")),
                json.dumps(row.get("raw") or {}, sort_keys=True),
            ),
        )
        _upsert_lifecycle(conn, row)


def _snapshot_row(item: dict[str, Any], stage: str, observed_at: str, scan_run_id: int | None) -> dict[str, Any] | None:
    over = item.get("over") if isinstance(item.get("over"), dict) else {}
    under = item.get("under") if isinstance(item.get("under"), dict) else {}
    over_book = item.get("over_book") or over.get("bookmaker")
    under_book = item.get("under_book") or under.get("bookmaker")
    player = item.get("player") or over.get("player") or under.get("player") or item.get("left_player") or item.get("right_player")
    prop_identity = item.get("prop_identity") or over.get("prop_identity") or under.get("prop_identity") or item.get("prop_type") or item.get("market_type") or item.get("left_market_type") or item.get("right_market_type")
    prop_period = item.get("prop_period") or over.get("prop_period") or under.get("prop_period") or "full_game"
    line = item.get("line") if item.get("line") is not None else over.get("line") if over.get("line") is not None else under.get("line") if under.get("line") is not None else item.get("left_line") if item.get("left_line") is not None else item.get("right_line")
    row = {
        "observed_at": observed_at,
        "scan_run_id": scan_run_id,
        "stage": stage,
        "provider": _provider(item, over, under),
        "over_book": over_book,
        "under_book": under_book,
        "sportsbook_pair": _pair(over_book, under_book),
        "player": player,
        "prop_identity": prop_identity,
        "prop_period": prop_period,
        "line": line,
        "over_odds": item.get("over_odds") or over.get("odds"),
        "under_odds": item.get("under_odds") or under.get("odds"),
        "implied_probability_sum": item.get("implied_probability_sum"),
        "roi": item.get("guaranteed_roi"),
        "rejection_reason": item.get("rejection_reason") if stage == "rejected" else None,
        "raw": item,
    }
    row["opportunity_id"] = stable_opportunity_id(row)
    return row


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _upsert_lifecycle(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    existing = conn.execute("SELECT * FROM opportunity_lifecycle WHERE opportunity_id = ?", (row["opportunity_id"],)).fetchone()
    roi = _num(row.get("roi"))
    if existing:
        conn.execute(
            """
            UPDATE opportunity_lifecycle
            SET last_seen = ?, seen_count = seen_count + 1, max_roi = MAX(COALESCE(max_roi, ?), ?),
                min_roi = MIN(COALESCE(min_roi, ?), ?), latest_roi = ?, status = 'active',
                inactive_at = NULL, raw_json = ?
            WHERE opportunity_id = ?
            """,
            (row["observed_at"], roi, roi, roi, roi, roi, json.dumps(row, sort_keys=True), row["opportunity_id"]),
        )
        return
    conn.execute(
        """
        INSERT INTO opportunity_lifecycle
        (opportunity_id, first_seen, last_seen, seen_count, max_roi, min_roi, latest_roi, provider, sportsbook_pair,
         over_book, under_book, player, prop_identity, prop_period, line, status, raw_json)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            row["opportunity_id"],
            row["observed_at"],
            row["observed_at"],
            roi,
            roi,
            roi,
            row.get("provider"),
            row.get("sportsbook_pair"),
            row.get("over_book"),
            row.get("under_book"),
            row.get("player"),
            row.get("prop_identity"),
            row.get("prop_period"),
            _num(row.get("line")),
            json.dumps(row, sort_keys=True),
        ),
    )


def _rank(rows: list[dict[str, Any]], field: str, lifetime_by_id: dict[str, float], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get(field) or "unknown")
        grouped.setdefault(key, []).append(row)
    ranked = []
    for key, items in grouped.items():
        rois = [float(item["roi"]) for item in items if item.get("roi") is not None]
        lifetimes = [lifetime_by_id.get(item["opportunity_id"], 0) for item in items]
        ranked.append({
            field: key,
            "appearances": len(items),
            "average_roi": round(sum(rois) / len(rois), 6) if rois else 0,
            "max_roi": round(max(rois), 6) if rois else 0,
            "median_roi": round(float(median(rois)), 6) if rois else 0,
            "average_lifetime_seconds": round(sum(lifetimes) / len(lifetimes), 3) if lifetimes else 0,
        })
    return sorted(ranked, key=lambda row: (row["appearances"], row["max_roi"]), reverse=True)[:limit]


def _histogram(values: list[float], cuts: list[float]) -> list[dict[str, Any]]:
    labels = [f"<{cuts[1]}"] + [f"{cuts[i]}-{cuts[i+1]}" for i in range(1, len(cuts) - 1)] + [f">={cuts[-1]}"]
    counts = [0 for _ in labels]
    for value in values:
        placed = False
        for idx in range(1, len(cuts)):
            if value < cuts[idx]:
                counts[idx - 1] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return [{"bucket": label, "count": count} for label, count in zip(labels, counts)]


def _provider(item: dict[str, Any], over: dict[str, Any], under: dict[str, Any]) -> str:
    over_source = item.get("over_source") or over.get("source") or over.get("provider")
    under_source = item.get("under_source") or under.get("source") or under.get("provider")
    if over_source and under_source and over_source != under_source:
        return f"{over_source}+{under_source}"
    return str(over_source or under_source or item.get("provider") or "odds-api")


def _pair(over_book: Any, under_book: Any) -> str:
    return f"{over_book or 'unknown'} / {under_book or 'unknown'}"


def _norm_line(value: Any) -> str:
    num = _num(value)
    return str(num if num is not None else value or "")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        text = str(value).strip()
        if text.startswith("+"):
            text = text[1:]
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _duration_seconds(start: Any, end: Any) -> float:
    first = _parse_time(start)
    last = _parse_time(end)
    if not first or not last:
        return 0.0
    return max(0.0, (last - first).total_seconds())
