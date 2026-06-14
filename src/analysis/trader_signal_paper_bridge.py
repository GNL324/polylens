from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, RECOMMENDATION_TYPES, init_trader_signal_db
from src.analysis.trader_signal_gates import apply_gate_to_recommendation, load_signal_family_stats
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

DEFAULT_MINIMUM_SCORE = 60.0
DEFAULT_NOTIONAL_USD = 10.0
DEFAULT_MAX_NOTIONAL_USD = 25.0
ELIGIBLE_RECOMMENDATION_TYPES = {"paper_entry", "watch"}
INTENT_STATUSES = ("blocked", "candidate", "simulated")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _signal_key_from_recommendation(recommendation_key: str) -> str:
    key = recommendation_key.removesuffix(":conflict")
    for recommendation_type in RECOMMENDATION_TYPES:
        suffix = f":{recommendation_type}"
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


@dataclass(frozen=True)
class PaperBridgeConfig:
    minimum_score: float = DEFAULT_MINIMUM_SCORE
    notional_usd: float = DEFAULT_NOTIONAL_USD
    max_notional_usd: float = DEFAULT_MAX_NOTIONAL_USD
    limit: int = 20


def init_trader_signal_paper_bridge_db(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> None:
    init_trader_signal_db(db_path)
    init_trader_signal_validation_db(db_path)
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trader_signal_paper_intents (
                intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_key TEXT NOT NULL UNIQUE,
                recommendation_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                trader_address TEXT NOT NULL,
                score REAL NOT NULL,
                validation_count INTEGER NOT NULL,
                historical_accuracy REAL,
                gate_status TEXT NOT NULL,
                gate_reason TEXT NOT NULL,
                notional_usd REAL NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_only INTEGER NOT NULL DEFAULT 1,
                paper_only INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_trader_signal_paper_intents_status ON trader_signal_paper_intents(status);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_paper_intents_trader ON trader_signal_paper_intents(trader_address);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_paper_intents_signal_type ON trader_signal_paper_intents(signal_type);
            """
        )


def _load_recommendations_with_gates(
    *,
    db_path: str | Path,
    limit: int,
) -> list[dict[str, Any]]:
    family_stats = load_signal_family_stats(db_path=db_path)
    with closing_connection(db_path) as conn:
        recommendation_rows = conn.execute(
            """
            SELECT recommendation_key, wallet, market_id, recommendation_type, signal_type, score, reason, created_at
            FROM trader_signal_recommendations
            ORDER BY score DESC, created_at DESC, recommendation_key ASC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
        signal_rows = conn.execute(
            """
            SELECT signal_key, side, market_title, asset
            FROM trader_signals
            """
        ).fetchall()
    signal_map = {str(row["signal_key"]): dict(row) for row in signal_rows}
    recommendations: list[dict[str, Any]] = []
    for row in recommendation_rows:
        signal_key = _signal_key_from_recommendation(str(row["recommendation_key"]))
        signal_row = signal_map.get(signal_key, {})
        base = {
            "recommendation_key": str(row["recommendation_key"]),
            "wallet": str(row["wallet"]),
            "market_id": str(row["market_id"]),
            "market_title": str(signal_row.get("market_title") or ""),
            "asset": str(signal_row.get("asset") or "OTHER"),
            "side": str(signal_row.get("side") or "unknown"),
            "recommendation_type": str(row["recommendation_type"]),
            "signal_type": str(row["signal_type"]),
            "score": float(row["score"] or 0.0),
            "reason": str(row["reason"] or ""),
        }
        recommendations.append(apply_gate_to_recommendation(base, family_stats))
    return recommendations


def _resolve_notional(score: float, config: PaperBridgeConfig) -> float:
    bounded = min(max(config.notional_usd, 0.0), config.max_notional_usd)
    if score >= 80:
        return round(min(config.max_notional_usd, bounded * 1.5), 4)
    return round(bounded, 4)


def build_paper_intent_from_recommendation(
    recommendation: dict[str, Any],
    *,
    config: PaperBridgeConfig | None = None,
) -> dict[str, Any]:
    config = config or PaperBridgeConfig()
    score = float(recommendation.get("score") or 0.0)
    gate_status = str(recommendation.get("gate_status") or "unproven")
    recommendation_type = str(recommendation.get("recommendation_type") or "")
    validation_count = int(recommendation.get("validation_count") or 0)
    historical_accuracy = recommendation.get("historical_accuracy")
    gate_reason = str(recommendation.get("gate_reason") or "")
    recommendation_id = str(recommendation.get("recommendation_key") or recommendation.get("recommendation_id") or "")

    status = "blocked"
    reason = "eligible for paper bridge review"
    if gate_status != "proven":
        reason = f"blocked: gate_status {gate_status} ({gate_reason})"
    elif recommendation_type not in ELIGIBLE_RECOMMENDATION_TYPES:
        reason = f"blocked: recommendation_type {recommendation_type} not eligible for paper bridge"
    elif score < config.minimum_score:
        reason = f"blocked: score {score} below minimum {config.minimum_score}"
    elif recommendation_type == "paper_entry":
        status = "simulated"
        reason = "simulated paper-copy intent from proven paper_entry recommendation"
    else:
        status = "candidate"
        reason = "candidate paper-copy intent from proven watch recommendation"

    notional_usd = _resolve_notional(score, config) if status != "blocked" else 0.0
    intent_key = f"{recommendation_id}:{status}"
    return {
        "intent_key": intent_key,
        "recommendation_id": recommendation_id,
        "market_id": str(recommendation.get("market_id") or ""),
        "side": str(recommendation.get("side") or "unknown"),
        "recommendation_type": recommendation_type,
        "signal_type": str(recommendation.get("signal_type") or ""),
        "trader_address": str(recommendation.get("wallet") or recommendation.get("trader_address") or ""),
        "score": score,
        "validation_count": validation_count,
        "historical_accuracy": historical_accuracy,
        "gate_status": gate_status,
        "gate_reason": gate_reason,
        "notional_usd": notional_usd,
        "status": status,
        "reason": reason,
        "created_at": _utc_now(),
        "read_only": True,
        "paper_only": True,
    }


def run_trader_signal_paper_bridge(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    config: PaperBridgeConfig | None = None,
) -> dict[str, Any]:
    config = config or PaperBridgeConfig()
    init_trader_signal_paper_bridge_db(db_path)
    recommendations = _load_recommendations_with_gates(db_path=db_path, limit=config.limit)
    intents = [build_paper_intent_from_recommendation(rec, config=config) for rec in recommendations]

    inserted = 0
    skipped = 0
    with closing_connection(db_path) as conn:
        for intent in intents:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO trader_signal_paper_intents (
                    intent_key, recommendation_id, market_id, side, recommendation_type,
                    signal_type, trader_address, score, validation_count, historical_accuracy,
                    gate_status, gate_reason, notional_usd, status, reason, created_at,
                    read_only, paper_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent["intent_key"],
                    intent["recommendation_id"],
                    intent["market_id"],
                    intent["side"],
                    intent["recommendation_type"],
                    intent["signal_type"],
                    intent["trader_address"],
                    float(intent["score"]),
                    int(intent["validation_count"]),
                    intent["historical_accuracy"],
                    intent["gate_status"],
                    intent["gate_reason"],
                    float(intent["notional_usd"]),
                    intent["status"],
                    intent["reason"],
                    intent["created_at"],
                    1,
                    1,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1

    blocked_count = sum(1 for intent in intents if intent["status"] == "blocked")
    candidate_count = sum(1 for intent in intents if intent["status"] == "candidate")
    simulated_count = sum(1 for intent in intents if intent["status"] == "simulated")
    return _with_flags(
        {
            "intents_created": inserted,
            "intents_skipped": skipped,
            "intents_built": len(intents),
            "blocked_count": blocked_count,
            "candidate_count": candidate_count,
            "simulated_count": simulated_count,
            "minimum_score": config.minimum_score,
            "notional_usd": config.notional_usd,
            "max_notional_usd": config.max_notional_usd,
            "intents": intents,
        }
    )


def _intent_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "intent_id": int(row["intent_id"]),
        "intent_key": row["intent_key"],
        "recommendation_id": row["recommendation_id"],
        "market_id": row["market_id"],
        "side": row["side"],
        "recommendation_type": row["recommendation_type"],
        "signal_type": row["signal_type"],
        "trader_address": row["trader_address"],
        "score": float(row["score"]),
        "validation_count": int(row["validation_count"]),
        "historical_accuracy": row["historical_accuracy"],
        "gate_status": row["gate_status"],
        "gate_reason": row["gate_reason"],
        "notional_usd": float(row["notional_usd"]),
        "status": row["status"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "read_only": bool(row["read_only"]),
        "paper_only": bool(row["paper_only"]),
    }


def trader_signal_paper_bridge_report(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    latest_limit: int = 10,
) -> dict[str, Any]:
    init_trader_signal_paper_bridge_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT intent_id, intent_key, recommendation_id, market_id, side, recommendation_type,
                   signal_type, trader_address, score, validation_count, historical_accuracy,
                   gate_status, gate_reason, notional_usd, status, reason, created_at,
                   read_only, paper_only
            FROM trader_signal_paper_intents
            ORDER BY created_at DESC, intent_id DESC
            """
        ).fetchall()
    intents = [_intent_row_to_dict(row) for row in rows]
    by_signal_type: dict[str, list[dict[str, Any]]] = {}
    by_trader: dict[str, list[dict[str, Any]]] = {}
    for intent in intents:
        by_signal_type.setdefault(str(intent["signal_type"]), []).append(intent)
        by_trader.setdefault(str(intent["trader_address"]), []).append(intent)

    scores = [float(intent["score"]) for intent in intents]
    accuracies = [
        float(intent["historical_accuracy"])
        for intent in intents
        if intent.get("historical_accuracy") is not None
    ]
    return _with_flags(
        {
            "total_intents": len(intents),
            "blocked": sum(1 for intent in intents if intent["status"] == "blocked"),
            "candidates": sum(1 for intent in intents if intent["status"] == "candidate"),
            "simulated": sum(1 for intent in intents if intent["status"] == "simulated"),
            "average_score": round(sum(scores) / len(scores), 4) if scores else None,
            "average_historical_accuracy": round(sum(accuracies) / len(accuracies), 4) if accuracies else None,
            "by_signal_type": [
                {
                    "signal_type": signal_type,
                    "count": len(items),
                    "blocked": sum(1 for item in items if item["status"] == "blocked"),
                    "candidates": sum(1 for item in items if item["status"] == "candidate"),
                    "simulated": sum(1 for item in items if item["status"] == "simulated"),
                }
                for signal_type, items in sorted(by_signal_type.items())
            ],
            "by_trader": [
                {
                    "trader_address": trader,
                    "count": len(items),
                    "blocked": sum(1 for item in items if item["status"] == "blocked"),
                    "candidates": sum(1 for item in items if item["status"] == "candidate"),
                    "simulated": sum(1 for item in items if item["status"] == "simulated"),
                }
                for trader, items in sorted(by_trader.items())
            ],
            "latest_intents": intents[: max(int(latest_limit), 0)],
        }
    )


def trader_signal_paper_bridge_summary(*, db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    report = trader_signal_paper_bridge_report(db_path=db_path, latest_limit=5)
    return {
        "total_intents": report["total_intents"],
        "blocked": report["blocked"],
        "candidates": report["candidates"],
        "simulated": report["simulated"],
        "average_score": report["average_score"],
        "average_historical_accuracy": report["average_historical_accuracy"],
    }
