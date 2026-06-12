from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection

DEFAULT_TRADERS_DB = "data/traders.db"
SCHEMA_VERSION = 1


@dataclass
class TraderRecord:
    wallet: str
    first_seen: str
    last_seen: str
    classification: str
    confidence: float
    watch_score: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "classification": self.classification,
            "confidence": self.confidence,
            "watch_score": self.watch_score,
        }


@dataclass
class WalletReportRecord:
    wallet: str
    report_timestamp: str
    classification: str
    confidence: float
    report_json: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "report_timestamp": self.report_timestamp,
            "classification": self.classification,
            "confidence": self.confidence,
            "report": json.loads(self.report_json),
        }


def calculate_watch_score(
    classification: str,
    confidence: float,
    metrics: dict[str, Any],
    signals: list[dict[str, Any]],
) -> int:
    if classification == "unknown":
        return 0

    base = int(float(confidence or 0.0) * 50)
    class_bonus = {
        "market_maker": 20,
        "arbitrage_trader": 20,
        "quantitative_directional": 15,
        "mixed": 18,
    }.get(classification, 10)

    signal_names = {str(signal.get("name") or "") for signal in signals or []}
    signal_score = 0
    if "high_merge_frequency" in signal_names:
        signal_score += 5
    if "high_overlap_frequency" in signal_names:
        signal_score += 5
    if "frequent_inventory_rebalancing" in signal_names:
        signal_score += 4
    if "two_sided_market_participation" in signal_names:
        signal_score += 3
    if "redeem_heavy_behavior" in signal_names:
        signal_score += 3

    metrics_score = 0
    arbitrage_volume = _safe_float(metrics.get("arbitrage_volume"))
    if arbitrage_volume >= 500:
        metrics_score += 5
    elif arbitrage_volume >= 100:
        metrics_score += 3
    elif arbitrage_volume >= 10:
        metrics_score += 1

    overlap_ratio = _safe_float(metrics.get("overlap_ratio"))
    if overlap_ratio >= 0.5:
        metrics_score += 3
    elif overlap_ratio >= 0.25:
        metrics_score += 2
    elif overlap_ratio >= 0.1:
        metrics_score += 1

    markets_traded = int(_safe_float(metrics.get("markets_traded")))
    if markets_traded >= 20:
        metrics_score += 2
    elif markets_traded >= 10:
        metrics_score += 1

    total_volume = sum(
        _safe_float(metrics.get(key))
        for key in ("buy_volume", "sell_volume", "redeem_volume", "merge_volume")
    )
    if total_volume >= 10_000:
        metrics_score += 3
    elif total_volume >= 1_000:
        metrics_score += 2
    elif total_volume >= 100:
        metrics_score += 1

    return min(100, max(0, base + class_bonus + signal_score + metrics_score))


def save_wallet_report(
    report: dict[str, Any],
    watch_score: int | None = None,
    db_path: str = DEFAULT_TRADERS_DB,
) -> None:
    _init_traders_db(db_path)
    wallet = str(report.get("wallet") or "").strip().lower()
    if not wallet:
        raise ValueError("wallet address is required")

    classification = str(report.get("classification") or "unknown")
    confidence = _safe_float(report.get("confidence"))
    if watch_score is None:
        watch_score = calculate_watch_score(
            classification,
            confidence,
            report.get("metrics") or {},
            report.get("signals") or [],
        )
    watch_score = int(watch_score)
    now = _now_iso()
    report_timestamp = str(report.get("timestamp") or report.get("report_timestamp") or now)
    report_json = json.dumps(report, sort_keys=True)

    with closing_connection(Path(db_path)) as conn:
        existing = conn.execute("SELECT first_seen, watch_score FROM wallets WHERE wallet = ?", (wallet,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE wallets
                SET last_seen = ?, classification = ?, confidence = ?, watch_score = ?, schema_version = ?
                WHERE wallet = ?
                """,
                (
                    now,
                    classification,
                    confidence,
                    max(int(existing["watch_score"]), watch_score),
                    SCHEMA_VERSION,
                    wallet,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO wallets (wallet, first_seen, last_seen, classification, confidence, watch_score, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (wallet, now, now, classification, confidence, watch_score, SCHEMA_VERSION),
            )

        conn.execute(
            """
            INSERT OR REPLACE INTO wallet_reports
            (wallet, report_timestamp, classification, confidence, report_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (wallet, report_timestamp, classification, confidence, report_json),
        )


def load_wallet_report(
    wallet: str,
    report_timestamp: str | None = None,
    db_path: str = DEFAULT_TRADERS_DB,
) -> WalletReportRecord | None:
    _init_traders_db(db_path)
    wallet = str(wallet or "").strip().lower()
    with closing_connection(Path(db_path)) as conn:
        if report_timestamp:
            row = conn.execute(
                """
                SELECT wallet, report_timestamp, classification, confidence, report_json
                FROM wallet_reports
                WHERE wallet = ? AND report_timestamp = ?
                """,
                (wallet, report_timestamp),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT wallet, report_timestamp, classification, confidence, report_json
                FROM wallet_reports
                WHERE wallet = ?
                ORDER BY report_timestamp DESC
                LIMIT 1
                """,
                (wallet,),
            ).fetchone()
    return _wallet_report_record(row) if row else None


def list_traders(
    classification: str | None = None,
    min_watch_score: int = 0,
    limit: int = 100,
    db_path: str = DEFAULT_TRADERS_DB,
) -> list[TraderRecord]:
    _init_traders_db(db_path)
    query = """
        SELECT wallet, first_seen, last_seen, classification, confidence, watch_score
        FROM wallets
        WHERE watch_score >= ?
    """
    params: list[Any] = [int(min_watch_score or 0)]
    if classification:
        query += " AND classification = ?"
        params.append(classification)
    query += " ORDER BY watch_score DESC, last_seen DESC, wallet ASC LIMIT ?"
    params.append(int(limit or 100))

    with closing_connection(Path(db_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_trader_record(row) for row in rows]


def top_traders(
    limit: int = 10,
    classification: str | None = None,
    db_path: str = DEFAULT_TRADERS_DB,
) -> list[TraderRecord]:
    return list_traders(classification=classification, min_watch_score=0, limit=limit, db_path=db_path)


def trader_summary(wallet: str, db_path: str = DEFAULT_TRADERS_DB) -> dict[str, Any] | None:
    _init_traders_db(db_path)
    wallet = str(wallet or "").strip().lower()
    with closing_connection(Path(db_path)) as conn:
        wallet_row = conn.execute(
            """
            SELECT wallet, first_seen, last_seen, classification, confidence, watch_score
            FROM wallets
            WHERE wallet = ?
            """,
            (wallet,),
        ).fetchone()
        if not wallet_row:
            return None
        report_rows = conn.execute(
            """
            SELECT report_timestamp, classification, confidence, report_json
            FROM wallet_reports
            WHERE wallet = ?
            ORDER BY report_timestamp DESC
            """,
            (wallet,),
        ).fetchall()
    return {
        **_trader_record(wallet_row).to_dict(),
        "report_count": len(report_rows),
        "reports": [
            {
                "report_timestamp": row["report_timestamp"],
                "classification": row["classification"],
                "confidence": row["confidence"],
                "report": json.loads(row["report_json"]),
            }
            for row in report_rows
        ],
    }


def registry_stats(db_path: str = DEFAULT_TRADERS_DB) -> dict[str, Any]:
    _init_traders_db(db_path)
    with closing_connection(Path(db_path)) as conn:
        total = conn.execute("SELECT COUNT(*) AS count FROM wallets").fetchone()["count"]
        classifications = conn.execute(
            """
            SELECT classification, COUNT(*) AS count, AVG(watch_score) AS avg_score
            FROM wallets
            GROUP BY classification
            ORDER BY count DESC, classification ASC
            """
        ).fetchall()
        top_scores = conn.execute(
            """
            SELECT wallet, classification, watch_score
            FROM wallets
            ORDER BY watch_score DESC, last_seen DESC, wallet ASC
            LIMIT 5
            """
        ).fetchall()
    return {
        "total_traders": total,
        "by_classification": [
            {
                "classification": row["classification"],
                "count": row["count"],
                "avg_watch_score": round(_safe_float(row["avg_score"]), 1),
            }
            for row in classifications
        ],
        "top_watch_scores": [
            {"wallet": row["wallet"], "classification": row["classification"], "watch_score": row["watch_score"]}
            for row in top_scores
        ],
    }


def _init_traders_db(db_path: str = DEFAULT_TRADERS_DB) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing_connection(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                wallet TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                classification TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                watch_score INTEGER NOT NULL DEFAULT 0,
                schema_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS wallet_reports (
                wallet TEXT NOT NULL,
                report_timestamp TEXT NOT NULL,
                classification TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                report_json TEXT NOT NULL,
                PRIMARY KEY (wallet, report_timestamp)
            );
            CREATE INDEX IF NOT EXISTS idx_wallets_watch_score ON wallets(watch_score DESC);
            CREATE INDEX IF NOT EXISTS idx_wallets_classification ON wallets(classification);
            CREATE INDEX IF NOT EXISTS idx_wallet_reports_wallet ON wallet_reports(wallet);
            """
        )


def _trader_record(row: Any) -> TraderRecord:
    return TraderRecord(
        wallet=row["wallet"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        classification=row["classification"],
        confidence=float(row["confidence"]),
        watch_score=int(row["watch_score"]),
    )


def _wallet_report_record(row: Any) -> WalletReportRecord:
    return WalletReportRecord(
        wallet=row["wallet"],
        report_timestamp=row["report_timestamp"],
        classification=row["classification"],
        confidence=float(row["confidence"]),
        report_json=row["report_json"],
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
