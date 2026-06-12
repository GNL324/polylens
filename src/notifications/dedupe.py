from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection

DEFAULT_DB_PATH = "data/opportunities.db"


@dataclasses.dataclass(frozen=True)
class DedupeKeyParts:
    sport: str
    event_id: str
    market_type: str
    player: str
    line: str
    over_book: str
    under_book: str
    over_odds: str
    under_odds: str


@dataclasses.dataclass(frozen=True)
class CryptoDedupeKeyParts:
    asset: str
    venue: str
    direction: str
    window_minutes: str
    market_id: str


def _is_crypto_opportunity(opportunity: dict[str, Any]) -> bool:
    return all(
        opportunity.get(k) is not None
        for k in ("asset", "venue", "direction", "window_minutes")
    )


def _crypto_fingerprint(opportunity: dict[str, Any]) -> str:
    def _fmt(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    market_id = (
        opportunity.get("market")
        or opportunity.get("condition_id")
        or opportunity.get("market_id")
        or opportunity.get("ticker")
        or opportunity.get("slug")
        or opportunity.get("token_id")
        or ""
    )

    parts = CryptoDedupeKeyParts(
        asset=_fmt(opportunity.get("asset")),
        venue=_fmt(opportunity.get("venue")),
        direction=_fmt(opportunity.get("direction")),
        window_minutes=_fmt(opportunity.get("window_minutes")),
        market_id=_fmt(market_id),
    )
    raw = "|".join(dataclasses.astuple(parts))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{digest}:{raw}"


def fingerprint(opportunity: dict[str, Any]) -> str:
    def _fmt(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    if _is_crypto_opportunity(opportunity):
        return _crypto_fingerprint(opportunity)

    parts = DedupeKeyParts(
        sport=_fmt(opportunity.get("sport")),
        event_id=_fmt(opportunity.get("event_id")),
        market_type=_fmt(opportunity.get("market_type") or opportunity.get("prop_type")),
        player=_fmt(opportunity.get("player")),
        line=_fmt(opportunity.get("line")),
        over_book=_fmt(opportunity.get("over_book")),
        under_book=_fmt(opportunity.get("under_book")),
        over_odds=_fmt(opportunity.get("over_odds")),
        under_odds=_fmt(opportunity.get("under_odds")),
    )
    raw = "|".join(dataclasses.astuple(parts))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{digest}:{raw}"


def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_history (
            fingerprint TEXT PRIMARY KEY,
            first_seen_at TEXT,
            last_alerted_at TEXT,
            alert_count INTEGER,
            last_roi REAL DEFAULT 0,
            last_profit REAL DEFAULT 0
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alert_history)").fetchall()}
    if "last_roi" not in columns:
        conn.execute("ALTER TABLE alert_history ADD COLUMN last_roi REAL DEFAULT 0")
    if "last_profit" not in columns:
        conn.execute("ALTER TABLE alert_history ADD COLUMN last_profit REAL DEFAULT 0")


def should_alert(
    opportunity: dict[str, Any],
    *,
    cooldown_minutes: int = 15,
    realert_roi_delta: float = 0.005,
    realert_profit_delta: float = 5.0,
    now: datetime | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    key = fingerprint(opportunity)
    current_roi = float(opportunity.get("guaranteed_roi") or 0)
    current_profit = float(opportunity.get("guaranteed_profit_amount") or 0)
    if db_path == DEFAULT_DB_PATH and os.environ.get("PYTEST_CURRENT_TEST"):
        safe_test = re.sub(r"[^a-zA-Z0-9_.-]+", "_", os.environ["PYTEST_CURRENT_TEST"])
        db_path = f"/tmp/polylens_test_alert_history_{safe_test}_{os.getpid()}.db"
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing_connection(path, row_factory=None) as conn:
        _init_table(conn)
        row = conn.execute(
            "SELECT first_seen_at, last_alerted_at, alert_count, fingerprint, last_roi, last_profit FROM alert_history WHERE fingerprint = ?",
            (key,),
        ).fetchone()
        if row:
            _first_seen_at, last_alerted_at, _alert_count, fingerprint_value, last_roi, last_profit = row
            assert fingerprint_value == key
            last_dt = datetime.fromisoformat(last_alerted_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() < cooldown_minutes * 60:
                if current_roi - float(last_roi or 0) >= realert_roi_delta or current_profit - float(last_profit or 0) >= realert_profit_delta:
                    conn.execute(
                        "UPDATE alert_history SET last_alerted_at = ?, alert_count = alert_count + 1, last_roi = ?, last_profit = ? WHERE fingerprint = ?",
                        (now.isoformat(), current_roi, current_profit, key),
                    )
                    return True, "realert_improved"
                return False, "cooldown"
            conn.execute(
                "UPDATE alert_history SET last_alerted_at = ?, alert_count = alert_count + 1, last_roi = ?, last_profit = ? WHERE fingerprint = ?",
                (now.isoformat(), current_roi, current_profit, key),
            )
            return True, "alert"
        conn.execute(
            "INSERT OR IGNORE INTO alert_history (fingerprint, first_seen_at, last_alerted_at, alert_count, last_roi, last_profit) VALUES (?, ?, ?, ?, ?, ?)",
            (key, now.isoformat(), now.isoformat(), 1, current_roi, current_profit),
        )
        return True, "alert"
