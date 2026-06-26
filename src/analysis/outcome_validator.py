from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.outcome_calibration import confidence_adjustments
from src.analysis.outcome_metrics import brier_score, rolling_group_metrics, validation_metrics
from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, init_paper_trading_db
from src.sqlite_utils import closing_connection


RESOLVED_STATUSES = {"correct", "incorrect", "partial"}


def init_outcome_validation_db(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> None:
    """Create additive, paper-only outcome validation tables."""
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outcome_validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                paper_position_id INTEGER,
                order_id INTEGER,
                opportunity_id TEXT,
                market_id TEXT,
                market_title TEXT,
                resolved_outcome TEXT,
                settlement_timestamp TEXT,
                market_duration_seconds REAL,
                confidence_at_entry REAL,
                predicted_side TEXT,
                actual_side TEXT,
                execution_price REAL,
                settlement_price REAL,
                strategy TEXT,
                copied_wallet TEXT,
                signal_family TEXT,
                validation_cohort TEXT,
                opportunity_score REAL,
                execution_quality REAL,
                portfolio_state_json TEXT NOT NULL DEFAULT '{}',
                validation_status TEXT NOT NULL,
                correctness_score REAL NOT NULL DEFAULT 0,
                brier_score REAL NOT NULL DEFAULT 0,
                market_category TEXT,
                market_liquidity REAL,
                time_to_expiry_seconds REAL,
                expiry_bucket TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_validations_source
                ON outcome_validations(source_table, source_id);
            CREATE INDEX IF NOT EXISTS idx_outcome_validations_status
                ON outcome_validations(validation_status, settlement_timestamp);
            CREATE INDEX IF NOT EXISTS idx_outcome_validations_wallet
                ON outcome_validations(copied_wallet, settlement_timestamp);
            CREATE INDEX IF NOT EXISTS idx_outcome_validations_strategy
                ON outcome_validations(strategy, settlement_timestamp);

            CREATE TABLE IF NOT EXISTS outcome_metric_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                dimension TEXT NOT NULL,
                label TEXT NOT NULL,
                metrics_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outcome_metric_snapshots_dimension
                ON outcome_metric_snapshots(dimension, label, created_at);

            CREATE TABLE IF NOT EXISTS outcome_backfill_state (
                key TEXT PRIMARY KEY,
                last_source_id INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )


def validate_completed_markets(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    limit: int | None = None,
    after_position_id: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate closed paper positions against their resolved settlement price."""
    init_outcome_validation_db(db_path)
    timestamp = _utc_now(now)
    candidates = _completed_canonical_positions(db_path, limit=limit, after_position_id=after_position_id)
    validations = [_validation_from_canonical(row, timestamp) for row in candidates]
    with closing_connection(db_path) as conn:
        for validation in validations:
            _upsert_validation(conn, validation)
    return {
        "paper_only": True,
        "checked": len(candidates),
        "validated": len(validations),
        "correct": sum(1 for item in validations if item["validation_status"] == "correct"),
        "incorrect": sum(1 for item in validations if item["validation_status"] == "incorrect"),
        "partial": sum(1 for item in validations if item["validation_status"] == "partial"),
        "unresolved": sum(1 for item in validations if item["validation_status"] == "unresolved"),
        "last_position_id": max([after_position_id, *[int(item.get("paper_position_id") or 0) for item in validations]], default=after_position_id),
    }


def rebuild_validation_metrics(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    batch_size: int = 250,
    reset: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resumable historical backfill from canonical resolved paper positions."""
    init_outcome_validation_db(db_path)
    key = "canonical_positions"
    with closing_connection(db_path) as conn:
        if reset:
            conn.execute("DELETE FROM outcome_validations WHERE source_table='paper_positions'")
            conn.execute("DELETE FROM outcome_backfill_state WHERE key=?", (key,))
        state = conn.execute("SELECT * FROM outcome_backfill_state WHERE key=?", (key,)).fetchone()
        after = int(state["last_source_id"]) if state is not None else 0
    batch = validate_completed_markets(db_path, limit=batch_size, after_position_id=after, now=now)
    status = "complete" if int(batch["checked"]) < max(1, int(batch_size)) else "running"
    with closing_connection(db_path) as conn:
        previous_count = 0
        if not reset:
            state = conn.execute("SELECT * FROM outcome_backfill_state WHERE key=?", (key,)).fetchone()
            previous_count = int(state["processed_count"]) if state is not None else 0
        conn.execute(
            """
            INSERT INTO outcome_backfill_state (key, last_source_id, processed_count, status, updated_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_source_id=excluded.last_source_id,
                processed_count=excluded.processed_count,
                status=excluded.status,
                updated_at=excluded.updated_at,
                raw_json=excluded.raw_json
            """,
            (
                key,
                int(batch["last_position_id"]),
                previous_count + int(batch["validated"]),
                status,
                _utc_now(now),
                json.dumps(batch, sort_keys=True),
            ),
        )
    return {**batch, "status": status, "resume_after_position_id": int(batch["last_position_id"])}


def outcome_validation_report(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    init_outcome_validation_db(db_path)
    now = _as_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=max(1, int(window_days)))
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    rows = _validation_rows(db_path)
    rolling_rows = [row for row in rows if _parse_time(row.get("settlement_timestamp")) is None or _parse_time(row.get("settlement_timestamp")) >= cutoff]
    today_rows = [row for row in rows if (_parse_time(row.get("settlement_timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= today_start]
    grouped = rolling_group_metrics(rolling_rows)
    summary = validation_metrics(rolling_rows)
    report = {
        "paper_only": True,
        "window_days": int(window_days),
        "total_validations": len(rows),
        "newly_resolved_markets": _recent_resolved(rows, limit=5),
        "todays_resolved_markets": len([row for row in today_rows if row.get("validation_status") in RESOLVED_STATUSES]),
        "todays_accuracy": validation_metrics(today_rows)["accuracy"],
        "validation_accuracy": summary["accuracy"],
        "precision": summary["precision"],
        "recall": summary["recall"],
        "brier_score": summary["brier_score"],
        "confidence_bias": summary["confidence_bias"],
        "calibration_drift": summary["calibration_drift"],
        "confidence_calibration": summary["calibration_curve"],
        "by_wallet": grouped["copied_wallet"],
        "by_strategy": grouped["strategy"],
        "by_signal_family": grouped["signal_family"],
        "by_market_category": grouped["market_category"],
        "by_expiry_bucket": grouped["expiry_bucket"],
        "confidence_adjustments": confidence_adjustments(grouped),
        "best_wallets": _rank(grouped["copied_wallet"].values(), reverse=True),
        "worst_wallets": _rank(grouped["copied_wallet"].values(), reverse=False),
        "best_strategies": _rank(grouped["strategy"].values(), reverse=True),
        "worst_strategies": _rank(grouped["strategy"].values(), reverse=False),
        "biggest_winners": _biggest(rows, status="correct"),
        "biggest_misses": _biggest(rows, status="incorrect"),
        "summary": summary,
    }
    _record_metric_snapshots(db_path, report, window_days=window_days, now=now)
    return report


def _completed_canonical_positions(db_path: str | Path, *, limit: int | None, after_position_id: int) -> list[dict[str, Any]]:
    with closing_connection(db_path) as conn:
        if not _table_exists(conn, "paper_positions"):
            return []
        limit_clause = "" if limit is None else f" LIMIT {max(0, int(limit))}"
        rows = conn.execute(
            f"""
            SELECT
                p.*,
                o.raw_json AS order_raw_json,
                o.simulated_price AS order_simulated_price,
                o.stake AS order_stake,
                s.exit_timestamp AS settlement_timestamp,
                s.exit_price AS settlement_price,
                s.reason AS settlement_reason
            FROM paper_positions p
            LEFT JOIN paper_orders o ON o.id = p.order_id
            LEFT JOIN paper_settlements s ON s.paper_position_id = p.paper_position_id
            WHERE p.status IN ('closed', 'expired') AND p.paper_position_id > ?
            ORDER BY p.paper_position_id ASC
            {limit_clause}
            """,
            (int(after_position_id),),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def _validation_from_canonical(row: dict[str, Any], timestamp: str) -> dict[str, Any]:
    raw = _json(row.get("order_raw_json"))
    predicted = _side(row.get("side"))
    settlement_price = _first_float(row.get("settlement_price"), row.get("exit_price"))
    status, actual_side, correctness = _classification(predicted, settlement_price)
    entry_at = _parse_time(row.get("entry_timestamp"))
    settled_at = _parse_time(row.get("settlement_timestamp") or row.get("exit_timestamp"))
    duration = (settled_at - entry_at).total_seconds() if entry_at and settled_at else None
    confidence = _probability(row.get("confidence") if row.get("confidence") is not None else raw.get("confidence"))
    category = str(row.get("market_category") or raw.get("market_category") or raw.get("category") or row.get("asset") or "unknown")
    expiry_seconds = _float(row.get("time_to_expiry_seconds") if row.get("time_to_expiry_seconds") is not None else raw.get("time_to_expiry_seconds"))
    expiry_bucket = _expiry_bucket(expiry_seconds)
    return {
        "source_table": "paper_positions",
        "source_id": int(row.get("paper_position_id") or 0),
        "paper_position_id": int(row.get("paper_position_id") or 0),
        "order_id": int(row.get("order_id") or 0) or None,
        "opportunity_id": row.get("opportunity_id"),
        "market_id": row.get("market_id"),
        "market_title": row.get("title"),
        "resolved_outcome": actual_side,
        "settlement_timestamp": _format_time(settled_at) if settled_at else None,
        "market_duration_seconds": duration,
        "confidence_at_entry": confidence,
        "predicted_side": predicted,
        "actual_side": actual_side,
        "execution_price": _first_float(row.get("entry_price"), row.get("order_simulated_price")),
        "settlement_price": settlement_price,
        "strategy": str(row.get("strategy") or raw.get("strategy") or "unknown"),
        "copied_wallet": row.get("source_wallet") or raw.get("source_wallet") or raw.get("wallet"),
        "signal_family": str(row.get("signal_family") or raw.get("signal_family") or row.get("strategy") or "unknown"),
        "validation_cohort": f"{category}:{expiry_bucket}",
        "opportunity_score": _first_float(raw.get("ranking_score"), raw.get("score"), raw.get("final_score")),
        "execution_quality": _execution_quality(raw),
        "portfolio_state_json": json.dumps(_portfolio_state(raw), sort_keys=True),
        "validation_status": status,
        "correctness_score": correctness,
        "brier_score": brier_score(confidence, correctness),
        "market_category": category,
        "market_liquidity": _first_float(row.get("market_liquidity"), raw.get("market_liquidity"), raw.get("liquidity")),
        "time_to_expiry_seconds": expiry_seconds,
        "expiry_bucket": expiry_bucket,
        "created_at": timestamp,
        "updated_at": timestamp,
        "raw_json": json.dumps({"source": "canonical_paper_position", "settlement_reason": row.get("settlement_reason"), "raw": raw}, sort_keys=True),
    }


def _upsert_validation(conn: Any, row: dict[str, Any]) -> None:
    columns = [
        "source_table", "source_id", "paper_position_id", "order_id", "opportunity_id", "market_id", "market_title",
        "resolved_outcome", "settlement_timestamp", "market_duration_seconds", "confidence_at_entry", "predicted_side",
        "actual_side", "execution_price", "settlement_price", "strategy", "copied_wallet", "signal_family",
        "validation_cohort", "opportunity_score", "execution_quality", "portfolio_state_json", "validation_status",
        "correctness_score", "brier_score", "market_category", "market_liquidity", "time_to_expiry_seconds",
        "expiry_bucket", "created_at", "updated_at", "raw_json",
    ]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{name}=excluded.{name}" for name in columns if name not in {"source_table", "source_id", "created_at"})
    conn.execute(
        f"""
        INSERT INTO outcome_validations ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(source_table, source_id) DO UPDATE SET {updates}
        """,
        tuple(row.get(column) for column in columns),
    )


def _validation_rows(db_path: str | Path) -> list[dict[str, Any]]:
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM outcome_validations ORDER BY settlement_timestamp DESC, id DESC").fetchall()
    return [_row_dict(row) for row in rows]


def _record_metric_snapshots(db_path: str | Path, report: dict[str, Any], *, window_days: int, now: datetime) -> None:
    created_at = _format_time(now)
    dimensions = {
        "summary": {"all": report["summary"]},
        "wallet": report["by_wallet"],
        "strategy": report["by_strategy"],
        "signal_family": report["by_signal_family"],
        "market_category": report["by_market_category"],
        "expiry_bucket": report["by_expiry_bucket"],
    }
    with closing_connection(db_path) as conn:
        for dimension, rows in dimensions.items():
            for label, metrics in rows.items():
                conn.execute(
                    "INSERT INTO outcome_metric_snapshots (created_at, window_days, dimension, label, metrics_json) VALUES (?, ?, ?, ?, ?)",
                    (created_at, int(window_days), dimension, str(label), json.dumps(metrics, sort_keys=True)),
                )


def _classification(predicted_side: str, settlement_price: float | None) -> tuple[str, str, float]:
    if settlement_price is None:
        return "unresolved", "unknown", 0.0
    price = min(1.0, max(0.0, float(settlement_price)))
    if price >= 0.999:
        return "correct", predicted_side, 1.0
    if price <= 0.001:
        return "incorrect", _opposite_side(predicted_side), 0.0
    return "partial", "partial", price


def _rank(items: Any, *, reverse: bool, limit: int = 5) -> list[dict[str, Any]]:
    rows = [dict(item) for item in items if int(item.get("resolved_count") or 0) > 0]
    rows.sort(key=lambda item: (float(item.get("accuracy") or 0.0), -float(item.get("brier_score") or 0.0), int(item.get("resolved_count") or 0)), reverse=reverse)
    return rows[:limit]


def _biggest(rows: list[dict[str, Any]], *, status: str, limit: int = 5) -> list[dict[str, Any]]:
    filtered = [row for row in rows if str(row.get("validation_status") or "") == status]
    filtered.sort(key=lambda row: abs(float(row.get("confidence_at_entry") or 0.0) - float(row.get("correctness_score") or 0.0)), reverse=True)
    return [
        {
            "market_title": row.get("market_title"),
            "strategy": row.get("strategy"),
            "copied_wallet": row.get("copied_wallet"),
            "confidence": row.get("confidence_at_entry"),
            "status": row.get("validation_status"),
            "brier_score": row.get("brier_score"),
        }
        for row in filtered[:limit]
    ]


def _recent_resolved(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if row.get("validation_status") not in RESOLVED_STATUSES:
            continue
        result.append(
            {
                "market_title": row.get("market_title"),
                "settlement_timestamp": row.get("settlement_timestamp"),
                "predicted_side": row.get("predicted_side"),
                "actual_side": row.get("actual_side"),
                "status": row.get("validation_status"),
                "confidence": row.get("confidence_at_entry"),
            }
        )
        if len(result) >= limit:
            break
    return result


def _portfolio_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = raw.get("portfolio_state")
    if isinstance(state, dict):
        return state
    return {
        key: raw.get(key)
        for key in ("cash_balance", "total_equity", "exposure", "open_positions")
        if raw.get(key) is not None
    }


def _execution_quality(raw: dict[str, Any]) -> float | None:
    execution = raw.get("execution")
    if isinstance(execution, dict):
        if execution.get("quality") is not None:
            return _float(execution.get("quality"))
        if execution.get("slippage") is not None:
            return round(max(0.0, 1.0 - abs(_float(execution.get("slippage")))), 6)
    return _first_float(raw.get("execution_quality"), raw.get("expected_execution_quality"))


def _expiry_bucket(seconds: float | None) -> str:
    value = float(seconds or 0.0)
    if value <= 0:
        return "unknown"
    hours = value / 3600
    if hours <= 24:
        return "0-1d"
    if hours <= 72:
        return "1-3d"
    if hours <= 168:
        return "3-7d"
    return "7d+"


def _side(value: Any) -> str:
    text = str(value or "yes").strip().lower()
    if text in {"no", "down", "short", "false"}:
        return "no"
    return "yes"


def _opposite_side(side: str) -> str:
    return "no" if _side(side) == "yes" else "yes"


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _probability(value: Any) -> float:
    number = _float(value)
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _json(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _table_exists(conn: Any, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _format_time(value: datetime | None) -> str:
    return _as_utc(value or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now(now: datetime | None = None) -> str:
    return _format_time(now or datetime.now(timezone.utc))


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
