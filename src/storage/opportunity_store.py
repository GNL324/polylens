from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.sqlite_utils import closing_connection

DEFAULT_DB_PATH = "data/polylens.db"


class OpportunityStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    scan_mode TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    venues_scanned_json TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    alert_count INTEGER NOT NULL DEFAULT 0,
                    skipped_summary_json TEXT NOT NULL,
                    error_summary_json TEXT NOT NULL,
                    raw_result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_run_id INTEGER,
                    timestamp TEXT NOT NULL,
                    venue_pair TEXT,
                    polymarket_id TEXT,
                    kalshi_ticker TEXT,
                    sportsbook_event_id TEXT,
                    market_titles_json TEXT NOT NULL,
                    raw_edge REAL,
                    execution_score REAL,
                    score_reason TEXT,
                    prices_odds_json TEXT NOT NULL,
                    close_time TEXT,
                    candidate_json TEXT NOT NULL,
                    FOREIGN KEY(scan_run_id) REFERENCES scan_runs(id)
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER,
                    scan_run_id INTEGER,
                    destination TEXT NOT NULL,
                    sent_timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
                    FOREIGN KEY(scan_run_id) REFERENCES scan_runs(id)
                );
                CREATE TABLE IF NOT EXISTS rejected_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_run_id INTEGER,
                    timestamp TEXT NOT NULL,
                    venue_pair TEXT,
                    market_titles_json TEXT NOT NULL,
                    rejection_reason TEXT NOT NULL,
                    parsed_fields_json TEXT NOT NULL,
                    diagnostic_json TEXT NOT NULL,
                    FOREIGN KEY(scan_run_id) REFERENCES scan_runs(id)
                );
                """
            )

    def save_scan_run(
        self,
        scan_mode: str,
        filters: dict[str, Any] | None = None,
        venues_scanned: dict[str, Any] | None = None,
        candidate_count: int = 0,
        alert_count: int = 0,
        skipped_summary: dict[str, Any] | None = None,
        error_summary: dict[str, Any] | None = None,
        raw_result: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> int:
        timestamp = timestamp or _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO scan_runs
                (timestamp, scan_mode, filters_json, venues_scanned_json, candidate_count, alert_count, skipped_summary_json, error_summary_json, raw_result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    scan_mode,
                    _json(filters or {}),
                    _json(venues_scanned or {}),
                    int(candidate_count or 0),
                    int(alert_count or 0),
                    _json(skipped_summary or {}),
                    _json(error_summary or {}),
                    _json(raw_result or {}),
                ),
            )
            return int(cur.lastrowid)

    def save_scan_result(self, result: dict[str, Any], scan_mode: str, filters: dict[str, Any] | None = None, alert_count: int = 0) -> tuple[int, list[int]]:
        scan_run_id = self.save_scan_run(
            scan_mode=scan_mode,
            filters=filters,
            venues_scanned=result.get("markets_scanned_by_venue") or {},
            candidate_count=int(result.get("candidates_after_filtering") or result.get("arbitrage_candidates_found") or 0),
            alert_count=alert_count,
            skipped_summary=result.get("skipped_rejected_reason_counts") or {},
            error_summary=result.get("filter_reasons") or {},
            raw_result=result,
        )
        opportunity_ids = [self.save_opportunity(candidate, scan_run_id=scan_run_id) for candidate in result.get("top_scored_candidates") or result.get("top_candidates") or []]
        return scan_run_id, opportunity_ids

    def save_opportunity(self, candidate: dict[str, Any], scan_run_id: int | None = None, timestamp: str | None = None) -> int:
        timestamp = timestamp or _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities
                (scan_run_id, timestamp, venue_pair, polymarket_id, kalshi_ticker, sportsbook_event_id, market_titles_json, raw_edge, execution_score, score_reason, prices_odds_json, close_time, candidate_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run_id,
                    timestamp,
                    candidate.get("venue_pair"),
                    candidate.get("polymarket_id"),
                    candidate.get("kalshi_ticker"),
                    candidate.get("sportsbook_event_id"),
                    _json(_market_titles(candidate)),
                    _num(candidate.get("raw_edge") if candidate.get("raw_edge") is not None else candidate.get("estimated_edge")),
                    _num(candidate.get("execution_score")),
                    candidate.get("score_reason"),
                    _json(_prices_odds(candidate)),
                    candidate.get("close_time") or candidate.get("commence_time") or candidate.get("expiration_time") or candidate.get("end_date"),
                    _json(candidate),
                ),
            )
            return int(cur.lastrowid)

    def save_alert(
        self,
        opportunity_id: int | None,
        destination: str,
        status: str,
        payload: dict[str, Any],
        scan_run_id: int | None = None,
        error: str | None = None,
        sent_timestamp: str | None = None,
    ) -> int:
        sent_timestamp = sent_timestamp or _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts
                (opportunity_id, scan_run_id, destination, sent_timestamp, status, error, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (opportunity_id, scan_run_id, destination, sent_timestamp, status, error, _json(payload)),
            )
            return int(cur.lastrowid)

    def save_rejected_candidate(self, diagnostic: dict[str, Any], scan_run_id: int | None = None, timestamp: str | None = None) -> int:
        timestamp = timestamp or _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO rejected_candidates
                (scan_run_id, timestamp, venue_pair, market_titles_json, rejection_reason, parsed_fields_json, diagnostic_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run_id,
                    timestamp,
                    diagnostic.get("venue_pair") or diagnostic.get("match_type"),
                    _json({"polymarket": diagnostic.get("polymarket_title"), "kalshi": diagnostic.get("kalshi_title")}),
                    diagnostic.get("reason") or diagnostic.get("rejection_reason") or "unknown",
                    _json({"polymarket": diagnostic.get("parsed_polymarket"), "kalshi": diagnostic.get("parsed_kalshi")}),
                    _json(diagnostic),
                ),
            )
            return int(cur.lastrowid)

    def recent_opportunities(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT id, scan_run_id, timestamp, venue_pair, polymarket_id, kalshi_ticker, sportsbook_event_id,
                   market_titles_json, raw_edge, execution_score, score_reason, prices_odds_json, close_time, candidate_json
            FROM opportunities ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT id, opportunity_id, scan_run_id, destination, sent_timestamp, status, error, payload_json
            FROM alerts ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            return {
                "scan_runs": conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0],
                "opportunities": conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
                "alerts": conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
                "rejected_candidates": conn.execute("SELECT COUNT(*) FROM rejected_candidates").fetchone()[0],
                "top_venue_pairs": [dict(row) for row in conn.execute("SELECT venue_pair, COUNT(*) AS count FROM opportunities GROUP BY venue_pair ORDER BY count DESC LIMIT 10")],
            }

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params)]
        for row in rows:
            for key in list(row):
                if key.endswith("_json"):
                    row[key[:-5]] = _loads(row.pop(key))
        return rows

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            yield conn


def _market_titles(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "polymarket": candidate.get("polymarket_title"),
        "kalshi": candidate.get("kalshi_title"),
        "sportsbook": candidate.get("sportsbook"),
        "sportsbook_team": candidate.get("sportsbook_team"),
        "sportsbook_opponent": candidate.get("sportsbook_opponent"),
    }


def _prices_odds(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "polymarket_yes": candidate.get("polymarket_implied_yes_price"),
        "kalshi_yes": candidate.get("kalshi_implied_yes_price"),
        "kalshi_no": candidate.get("kalshi_implied_no_price"),
        "sportsbook_probability": candidate.get("sportsbook_implied_probability"),
        "odds": candidate.get("odds"),
        "spread": candidate.get("spread"),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
