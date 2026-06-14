from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.trader_signal_engine import (
    DEFAULT_TRADER_SIGNAL_DB,
    RECOMMENDATION_TYPES,
    init_trader_signal_db,
    load_outcomes_json,
)
from src.sqlite_utils import closing_connection

CONFIDENCE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-50", 0.0, 50.0),
    ("50-70", 50.0, 70.0),
    ("70-85", 70.0, 85.0),
    ("85-100", 85.0, 100.01),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if text in {"up", "down", "yes", "no"}:
        return text
    return text


def _parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _timestamp_to_iso(value: Any) -> str | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return str(value) if value not in (None, "") else None
    return datetime.fromtimestamp(parsed, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _signal_key_from_recommendation(recommendation_key: str) -> str:
    key = recommendation_key.removesuffix(":conflict")
    for recommendation_type in RECOMMENDATION_TYPES:
        suffix = f":{recommendation_type}"
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def init_trader_signal_validation_db(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> None:
    init_trader_signal_db(db_path)
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trader_signal_validation (
                validation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                validation_key TEXT NOT NULL UNIQUE,
                recommendation_id TEXT,
                signal_id TEXT,
                market_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                recommendation_type TEXT,
                generated_at TEXT,
                resolved_at TEXT,
                outcome TEXT NOT NULL,
                predicted_side TEXT,
                correct INTEGER NOT NULL,
                roi_proxy REAL,
                confidence REAL,
                trader_address TEXT NOT NULL,
                validation_timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trader_signal_validation_market ON trader_signal_validation(market_id);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_validation_trader ON trader_signal_validation(trader_address);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_validation_signal_type ON trader_signal_validation(signal_type);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_validation_recommendation_type ON trader_signal_validation(recommendation_type);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_validation_resolved_at ON trader_signal_validation(resolved_at);
            """
        )


def _outcome_key(row: dict[str, Any], market_id: str, wallet: str) -> str:
    explicit = str(row.get("outcome_key") or row.get("id") or "").strip()
    if explicit:
        return explicit
    return f"{wallet}:{market_id}:{row.get('resolved_at') or row.get('outcome') or 'unknown'}"


def _outcome_market_id(row: dict[str, Any]) -> str:
    for key in ("market_id", "condition_id", "conditionId", "market", "slug"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _outcome_wallet(row: dict[str, Any]) -> str:
    return _normalize_wallet(str(row.get("wallet") or row.get("trader_address") or row.get("trader") or ""))


def _winning_side(row: dict[str, Any]) -> str:
    for key in ("winning_side", "resolved_side", "winning_outcome", "side", "outcome_side"):
        value = _normalize_side(row.get(key))
        if value in {"up", "down", "yes", "no"}:
            return value
    outcome = str(row.get("outcome") or "").strip().lower()
    if outcome in {"up", "down", "yes", "no"}:
        return outcome
    return ""


def _roi_proxy(row: dict[str, Any]) -> float | None:
    for key in ("roi_proxy", "roi", "return", "pnl_ratio"):
        if row.get(key) is not None and row.get(key) != "":
            try:
                return round(float(row.get(key)), 6)
            except (TypeError, ValueError):
                continue
    entry = row.get("entry_price")
    exit_price = row.get("exit_price")
    if entry not in (None, "") and exit_price not in (None, ""):
        try:
            entry_f = float(entry)
            exit_f = float(exit_price)
            if entry_f > 0:
                return round((exit_f - entry_f) / entry_f, 6)
        except (TypeError, ValueError):
            return None
    return None


def _outcome_label(row: dict[str, Any]) -> str:
    return str(row.get("outcome") or row.get("result") or "unknown").strip().lower()


def _confidence_bucket(confidence: float | None) -> str:
    score = float(confidence or 0.0)
    for label, lower, upper in CONFIDENCE_BUCKETS:
        if lower <= score < upper:
            return label
    return "85-100"


def _is_correct(
    *,
    recommendation_type: str | None,
    signal_type: str,
    predicted_side: str,
    winning_side: str,
    outcome_label: str,
    roi_proxy: float | None,
) -> int | None:
    if recommendation_type in {"paper_exit", "exit"} or signal_type == "exit":
        if roi_proxy is not None:
            return 1 if roi_proxy >= 0 else 0
        return 1 if outcome_label in {"win", "correct", "success"} else 0 if outcome_label in {"loss", "incorrect", "fail"} else None
    if recommendation_type == "avoid":
        if winning_side and predicted_side:
            return 0 if predicted_side == winning_side else 1
        if roi_proxy is not None:
            return 1 if roi_proxy <= 0 else 0
        return None
    if winning_side and predicted_side:
        return 1 if predicted_side == winning_side else 0
    if roi_proxy is not None:
        return 1 if roi_proxy >= 0 else 0
    if outcome_label in {"win", "correct", "success"}:
        return 1
    if outcome_label in {"loss", "incorrect", "fail"}:
        return 0
    return None


def _build_validation_row(
    *,
    source_kind: str,
    source_id: str,
    recommendation_id: str | None,
    signal_id: str | None,
    market_id: str,
    signal_type: str,
    recommendation_type: str | None,
    generated_at: str | None,
    outcome_row: dict[str, Any],
    predicted_side: str,
    confidence: float | None,
    trader_address: str,
) -> dict[str, Any] | None:
    outcome_market = _outcome_market_id(outcome_row)
    if outcome_market and outcome_market != market_id:
        return None
    outcome_wallet = _outcome_wallet(outcome_row)
    if outcome_wallet and trader_address and outcome_wallet != trader_address:
        return None
    if str(outcome_row.get("signal_key") or "").strip() and signal_id:
        if str(outcome_row.get("signal_key")).strip() != signal_id:
            return None
    if str(outcome_row.get("recommendation_key") or "").strip() and recommendation_id:
        if str(outcome_row.get("recommendation_key")).strip() != recommendation_id:
            return None

    outcome_key = _outcome_key(outcome_row, market_id, trader_address)
    validation_key = f"{source_kind}:{source_id}:{outcome_key}"
    winning_side = _winning_side(outcome_row)
    roi_proxy = _roi_proxy(outcome_row)
    outcome_label = _outcome_label(outcome_row)
    correct = _is_correct(
        recommendation_type=recommendation_type,
        signal_type=signal_type,
        predicted_side=_normalize_side(predicted_side),
        winning_side=_normalize_side(winning_side),
        outcome_label=outcome_label,
        roi_proxy=roi_proxy,
    )
    if correct is None:
        return None
    return {
        "validation_key": validation_key,
        "recommendation_id": recommendation_id,
        "signal_id": signal_id,
        "market_id": market_id,
        "signal_type": signal_type,
        "recommendation_type": recommendation_type,
        "generated_at": generated_at,
        "resolved_at": _timestamp_to_iso(outcome_row.get("resolved_at") or outcome_row.get("timestamp")),
        "outcome": outcome_label or "unknown",
        "predicted_side": _normalize_side(predicted_side) or None,
        "correct": int(correct),
        "roi_proxy": roi_proxy,
        "confidence": confidence,
        "trader_address": trader_address,
        "validation_timestamp": _utc_now(),
    }


def _index_outcomes(outcomes: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        market_id = _outcome_market_id(row)
        wallet = _outcome_wallet(row)
        if market_id:
            indexed[(market_id, wallet)].append(row)
            if wallet:
                indexed[(market_id, "")].append(row)
        signal_key = str(row.get("signal_key") or "").strip()
        recommendation_key = str(row.get("recommendation_key") or "").strip()
        if signal_key:
            indexed[(f"signal:{signal_key}", "")].append(row)
        if recommendation_key:
            indexed[(f"rec:{recommendation_key}", "")].append(row)
    return indexed


def _candidate_outcomes(
    indexed: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    market_id: str,
    wallet: str,
    signal_id: str | None = None,
    recommendation_id: str | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    keys = [
        (market_id, wallet),
        (market_id, ""),
        (f"signal:{signal_id}", "") if signal_id else None,
        (f"rec:{recommendation_id}", "") if recommendation_id else None,
    ]
    for key in keys:
        if not key:
            continue
        for row in indexed.get(key, []):
            marker = json.dumps(row, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            candidates.append(row)
    return candidates


def validate_trader_signals(
    outcomes: list[dict[str, Any]] | None = None,
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    init_trader_signal_validation_db(db_path)
    outcomes = outcomes or []
    indexed = _index_outcomes(outcomes)
    rows_to_insert: list[dict[str, Any]] = []

    with closing_connection(db_path) as conn:
        recommendations = conn.execute(
            """
            SELECT recommendation_key, wallet, market_id, recommendation_type, signal_type, score, created_at
            FROM trader_signal_recommendations
            ORDER BY created_at ASC, recommendation_key ASC
            """
        ).fetchall()
        signals = conn.execute(
            """
            SELECT signal_key, wallet, market_id, signal_type, side, score, created_at
            FROM trader_signals
            ORDER BY created_at ASC, signal_key ASC
            """
        ).fetchall()

    signal_map = {str(row["signal_key"]): dict(row) for row in signals}
    recommendation_signal_keys = {
        _signal_key_from_recommendation(str(row["recommendation_key"])) for row in recommendations
    }

    for row in recommendations:
        recommendation_id = str(row["recommendation_key"])
        wallet = _normalize_wallet(str(row["wallet"]))
        market_id = str(row["market_id"])
        signal_id = _signal_key_from_recommendation(recommendation_id)
        signal_row = signal_map.get(signal_id, {})
        predicted_side = str(signal_row.get("side") or "")
        for outcome_row in _candidate_outcomes(
            indexed,
            market_id=market_id,
            wallet=wallet,
            signal_id=signal_id,
            recommendation_id=recommendation_id,
        ):
            validation_row = _build_validation_row(
                source_kind="rec",
                source_id=recommendation_id,
                recommendation_id=recommendation_id,
                signal_id=signal_id,
                market_id=market_id,
                signal_type=str(row["signal_type"]),
                recommendation_type=str(row["recommendation_type"]),
                generated_at=str(row["created_at"]),
                outcome_row=outcome_row,
                predicted_side=predicted_side,
                confidence=float(row["score"] or 0.0),
                trader_address=wallet,
            )
            if validation_row:
                rows_to_insert.append(validation_row)

    for row in signals:
        signal_id = str(row["signal_key"])
        if signal_id in recommendation_signal_keys:
            continue
        wallet = _normalize_wallet(str(row["wallet"]))
        market_id = str(row["market_id"])
        for outcome_row in _candidate_outcomes(
            indexed,
            market_id=market_id,
            wallet=wallet,
            signal_id=signal_id,
        ):
            validation_row = _build_validation_row(
                source_kind="sig",
                source_id=signal_id,
                recommendation_id=None,
                signal_id=signal_id,
                market_id=market_id,
                signal_type=str(row["signal_type"]),
                recommendation_type=None,
                generated_at=str(row["created_at"]),
                outcome_row=outcome_row,
                predicted_side=str(row["side"] or outcome_row.get("predicted_side") or ""),
                confidence=float(row["score"] or 0.0) if row["score"] is not None else None,
                trader_address=wallet,
            )
            if validation_row:
                rows_to_insert.append(validation_row)

    inserted = 0
    skipped = 0
    unmatched_outcomes = max(0, len(outcomes) - len({row["validation_key"] for row in rows_to_insert}))
    with closing_connection(db_path) as conn:
        for row in rows_to_insert:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO trader_signal_validation (
                    validation_key, recommendation_id, signal_id, market_id, signal_type,
                    recommendation_type, generated_at, resolved_at, outcome, predicted_side,
                    correct, roi_proxy, confidence, trader_address, validation_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["validation_key"],
                    row["recommendation_id"],
                    row["signal_id"],
                    row["market_id"],
                    row["signal_type"],
                    row["recommendation_type"],
                    row["generated_at"],
                    row["resolved_at"],
                    row["outcome"],
                    row["predicted_side"],
                    row["correct"],
                    row["roi_proxy"],
                    row["confidence"],
                    row["trader_address"],
                    row["validation_timestamp"],
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1

    return _with_flags(
        {
            "outcomes_received": len(outcomes),
            "recommendations_seen": len(recommendations),
            "signals_seen": len(signals),
            "validations_built": len(rows_to_insert),
            "validations_inserted": inserted,
            "validations_skipped": skipped,
            "unmatched_outcomes": unmatched_outcomes,
        }
    )


def validate_trader_signals_from_path(
    outcomes_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    outcomes = load_outcomes_json(outcomes_path)
    result = validate_trader_signals(outcomes, db_path=db_path)
    result["outcomes_path"] = str(outcomes_path)
    return result


def _accuracy(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    correct = sum(int(row["correct"]) for row in rows)
    return round(correct / len(rows), 6)


def _avg_roi(rows: list[dict[str, Any]]) -> float | None:
    values = [float(row["roi_proxy"]) for row in rows if row.get("roi_proxy") is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _group_performance(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        label = str(row.get(key) or "unknown")
        grouped[label].append(row)
    performance = []
    for label, items in sorted(grouped.items()):
        performance.append(
            {
                key: label,
                "count": len(items),
                "correct": sum(int(item["correct"]) for item in items),
                "incorrect": len(items) - sum(int(item["correct"]) for item in items),
                "accuracy": _accuracy(items),
                "avg_roi_proxy": _avg_roi(items),
            }
        )
    performance.sort(key=lambda item: (-(item["count"]), item[key]))
    return performance


def _trader_performance(rows: list[dict[str, Any]], *, reverse: bool = False, limit: int = 5) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["trader_address"])].append(row)
    ranked = []
    for trader, items in grouped.items():
        ranked.append(
            {
                "trader_address": trader,
                "count": len(items),
                "accuracy": _accuracy(items),
                "avg_roi_proxy": _avg_roi(items),
            }
        )
    ranked = [item for item in ranked if item["accuracy"] is not None]
    ranked.sort(key=lambda item: (item["accuracy"], item["count"]), reverse=reverse)
    return ranked[:limit]


def _rolling_window(rows: list[dict[str, Any]], days: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    window_rows = []
    for row in rows:
        resolved = _parse_timestamp(row.get("resolved_at"))
        if resolved is None:
            continue
        if datetime.fromtimestamp(resolved, tz=timezone.utc) >= cutoff:
            window_rows.append(row)
    return {
        "days": days,
        "count": len(window_rows),
        "accuracy": _accuracy(window_rows),
        "avg_roi_proxy": _avg_roi(window_rows),
    }


def trader_signal_validation_report(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    now: datetime | None = None,
) -> dict[str, Any]:
    init_trader_signal_validation_db(db_path)
    with closing_connection(db_path) as conn:
        raw_rows = conn.execute(
            """
            SELECT validation_key, recommendation_id, signal_id, market_id, signal_type,
                   recommendation_type, generated_at, resolved_at, outcome, predicted_side,
                   correct, roi_proxy, confidence, trader_address, validation_timestamp
            FROM trader_signal_validation
            ORDER BY validation_timestamp ASC, validation_key ASC
            """
        ).fetchall()
    rows = [dict(row) for row in raw_rows]
    consensus_rows = [row for row in rows if row["signal_type"] == "consensus"]
    contrarian_rows = [row for row in rows if row["signal_type"] == "contrarian"]
    confidence_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        confidence_rows[_confidence_bucket(row.get("confidence"))].append(row)

    return _with_flags(
        {
            "total_validated": len(rows),
            "overall_accuracy": _accuracy(rows),
            "signal_type_performance": _group_performance(rows, "signal_type"),
            "recommendation_type_performance": _group_performance(rows, "recommendation_type"),
            "consensus_performance": {
                "count": len(consensus_rows),
                "accuracy": _accuracy(consensus_rows),
                "avg_roi_proxy": _avg_roi(consensus_rows),
            },
            "contrarian_performance": {
                "count": len(contrarian_rows),
                "accuracy": _accuracy(contrarian_rows),
                "avg_roi_proxy": _avg_roi(contrarian_rows),
            },
            "confidence_bucket_performance": [
                {
                    "bucket": bucket,
                    "count": len(confidence_rows[bucket]),
                    "accuracy": _accuracy(confidence_rows[bucket]),
                    "avg_roi_proxy": _avg_roi(confidence_rows[bucket]),
                }
                for bucket, _, _ in CONFIDENCE_BUCKETS
            ],
            "top_traders": _trader_performance(rows, reverse=True),
            "worst_traders": _trader_performance(rows, reverse=False),
            "rolling_7_day": _rolling_window(rows, 7, now=now),
            "rolling_30_day": _rolling_window(rows, 30, now=now),
        }
    )
