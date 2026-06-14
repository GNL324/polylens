from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection

DEFAULT_TRADER_SIGNAL_DB = "data/trader_signals.db"
SIGNAL_TYPES = ("early_entry", "conviction", "exit", "consensus", "contrarian")
RECOMMENDATION_TYPES = ("watch", "paper_entry", "paper_exit", "avoid", "blocked")
SIGNAL_TYPE_WEIGHTS = {
    "early_entry": 60.0,
    "conviction": 70.0,
    "exit": 50.0,
    "consensus": 75.0,
    "contrarian": 55.0,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def init_trader_signal_db(db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trader_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT NOT NULL UNIQUE,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_title TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                shares REAL NOT NULL,
                amount REAL NOT NULL,
                score REAL,
                tx_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signal_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT NOT NULL,
                outcome_key TEXT NOT NULL UNIQUE,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                roi REAL,
                outcome TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(signal_key, outcome_key)
            );
            CREATE TABLE IF NOT EXISTS trader_signal_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_key TEXT NOT NULL UNIQUE,
                wallet TEXT NOT NULL,
                market_id TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                conflict INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trader_signal_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_timestamp TEXT NOT NULL,
                activity_path TEXT,
                outcomes_path TEXT,
                signals_generated INTEGER NOT NULL,
                signals_persisted INTEGER NOT NULL,
                signals_scored INTEGER NOT NULL,
                performance_updated INTEGER NOT NULL,
                recommendations_generated INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trader_signals_wallet ON trader_signals(wallet);
            CREATE INDEX IF NOT EXISTS idx_trader_signals_market ON trader_signals(market_id);
            CREATE INDEX IF NOT EXISTS idx_trader_signals_type ON trader_signals(signal_type);
            CREATE INDEX IF NOT EXISTS idx_trader_signals_score ON trader_signals(score);
            CREATE INDEX IF NOT EXISTS idx_signal_performance_wallet ON signal_performance(wallet);
            CREATE INDEX IF NOT EXISTS idx_signal_performance_signal ON signal_performance(signal_key);
            CREATE INDEX IF NOT EXISTS idx_trader_signal_recommendations_wallet ON trader_signal_recommendations(wallet);
            """
        )


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _parse_timestamp(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
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
        return 0.0


def _normalize_action(row: dict[str, Any]) -> str:
    explicit = str(_first_present(row, ("action", "side_effect")) or "").strip().lower()
    if explicit in {"buy", "sell", "redeem", "merge"}:
        return explicit
    activity_type = str(_first_present(row, ("type", "event_type")) or "").upper()
    if activity_type == "TRADE":
        side = str(row.get("side") or "").strip().lower()
        if side in {"buy", "sell"}:
            return side
        return ""
    if activity_type == "REDEEM":
        return "redeem"
    if activity_type == "MERGE":
        return "merge"
    return ""


def _market_id(row: dict[str, Any]) -> str:
    value = _first_present(
        row,
        ("market_id", "condition_id", "conditionId", "market", "slug", "market_slug", "eventSlug", "event_slug"),
    )
    if value:
        return str(value).strip()
    title = str(_first_present(row, ("market_title", "title")) or "").strip()
    return title or "unknown"


def _normalize_side(row: dict[str, Any]) -> str:
    for key in ("outcome", "position", "token_side"):
        value = str(row.get(key) or "").strip().lower().rstrip(".")
        if value in {"up", "down", "yes", "no"}:
            return value
        if value and value not in {"buy", "sell"}:
            return value
    raw_side = str(row.get("side") or "").strip().lower()
    if raw_side in {"up", "down", "yes", "no"}:
        return raw_side
    return "unknown"


def _infer_asset(row: dict[str, Any], title: str, slug: str) -> str:
    explicit = str(row.get("asset") or "").strip().upper()
    if explicit in {"BTC", "ETH", "SOL"}:
        return explicit
    text = f"{title} {slug}".upper()
    for asset, terms in (("BTC", ("BTC", "BITCOIN")), ("ETH", ("ETH", "ETHEREUM")), ("SOL", ("SOL", "SOLANA"))):
        if any(term in text for term in terms):
            return asset
    return str(row.get("asset") or "OTHER")


def _extract_activity_rows(payload: Any) -> tuple[list[Any], str]:
    default_wallet = ""
    if isinstance(payload, list):
        return payload, default_wallet
    if isinstance(payload, dict):
        default_wallet = _normalize_wallet(
            str(_first_present(payload, ("wallet", "wallet_address")) or "")
        )
        for key in ("events", "activities", "activity"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, default_wallet
    raise ValueError("activity JSON must be a list of events or an export object with events or activities")


def load_activity_json(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows, default_wallet = _extract_activity_rows(payload)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = _normalize_event(row, default_wallet=default_wallet)
        if event.get("action"):
            normalized.append(event)
    return normalized


def load_outcomes_json(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        outcomes = payload.get("outcomes")
        if isinstance(outcomes, list):
            return [row for row in outcomes if isinstance(row, dict)]
    raise ValueError("outcomes JSON must be a list or an object with outcomes")


def _normalize_event(row: dict[str, Any], *, default_wallet: str = "") -> dict[str, Any]:
    wallet = _normalize_wallet(
        str(_first_present(row, ("wallet", "proxyWallet", "proxy_wallet")) or default_wallet or "")
    )
    action = _normalize_action(row)
    timestamp = _parse_timestamp(_first_present(row, ("timestamp", "created_at", "time", "datetime")))
    market_id = _market_id(row)
    slug = str(_first_present(row, ("market_slug", "slug", "eventSlug", "event_slug")) or "")
    market_title = str(
        _first_present(row, ("market_title", "title")) or slug or market_id or "unknown"
    )
    price = _safe_float(_first_present(row, ("price", "avg_price", "average_price")))
    shares = _safe_float(_first_present(row, ("shares", "size", "quantity", "volume")))
    amount = _safe_float(_first_present(row, ("amount", "usdcSize", "usdc_size")))
    if amount <= 0 and shares > 0 and price > 0:
        amount = round(shares * price, 6)
    side = _normalize_side(row)
    asset = _infer_asset(row, market_title, slug)
    tx_hash = str(
        _first_present(row, ("tx_hash", "transactionHash", "transaction_hash", "hash"))
        or f"{wallet}:{timestamp}:{action}:{market_id}:{side}"
    )
    return {
        "wallet": wallet,
        "timestamp": timestamp,
        "event_type": str(_first_present(row, ("event_type", "type")) or action),
        "market_id": market_id,
        "condition_id": str(_first_present(row, ("condition_id", "conditionId", "market_id")) or market_id),
        "market_slug": slug,
        "market_title": market_title,
        "asset": asset,
        "side": side,
        "action": action,
        "shares": shares,
        "price": price,
        "amount": amount,
        "tx_hash": tx_hash,
        "raw": row.get("raw") if isinstance(row.get("raw"), dict) else row,
    }


def _signal_key(event: dict[str, Any], signal_type: str) -> str:
    return ":".join(
        [
            event.get("wallet") or "unknown",
            event.get("market_id") or "unknown",
            signal_type,
            str(event.get("timestamp") or 0),
            event.get("tx_hash") or "unknown",
        ]
    )


def generate_signals_from_activity(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return []

    buy_amounts = [
        float(event["amount"])
        for event in events
        if event.get("action") == "buy" and float(event.get("amount") or 0) > 0
    ]
    median_buy = statistics.median(buy_amounts) if buy_amounts else 0.0
    first_buy_by_market: set[str] = set()
    market_buyers: dict[str, set[str]] = {}
    signals: list[dict[str, Any]] = []
    exit_actions = {"sell", "redeem", "merge"}

    for event in sorted(events, key=lambda row: (float(row.get("timestamp") or 0), row.get("tx_hash") or "")):
        action = event.get("action")
        market_id = str(event.get("market_id") or "")
        wallet = str(event.get("wallet") or "")
        price = float(event.get("price") or 0.0)
        amount = float(event.get("amount") or 0.0)
        shares = float(event.get("shares") or 0.0)
        size_metric = amount if amount > 0 else shares

        if action == "buy" and market_id and market_id != "unknown":
            market_buyers.setdefault(market_id, set()).add(wallet)
            if market_id not in first_buy_by_market:
                first_buy_by_market.add(market_id)
                signals.append(_build_signal(event, "early_entry", "first buy in market for wallet activity export"))
            if median_buy > 0 and amount >= median_buy * 2:
                signals.append(_build_signal(event, "conviction", "buy size at or above 2x median buy amount"))
            elif size_metric >= 100:
                signals.append(_build_signal(event, "conviction", "large buy or position size"))
            elif size_metric >= 50:
                signals.append(_build_signal(event, "conviction", "elevated buy or position size"))
            if price >= 0.65:
                signals.append(_build_signal(event, "contrarian", "buy at elevated price"))
        elif action in exit_actions:
            reason = {
                "sell": "wallet reduced or exited exposure",
                "redeem": "wallet redeemed resolved position",
                "merge": "wallet merged paired positions",
            }[action]
            signals.append(_build_signal(event, "exit", reason))
            if action == "sell" and price <= 0.35:
                signals.append(_build_signal(event, "contrarian", "sell at depressed price"))

    for market_id, wallets in market_buyers.items():
        if len(wallets) < 2:
            continue
        sample = next(event for event in events if event.get("market_id") == market_id and event.get("action") == "buy")
        consensus = _build_signal(
            sample,
            "consensus",
            f"multi-wallet buy consensus ({len(wallets)} wallets)",
        )
        consensus["consensus_wallet_count"] = len(wallets)
        signals.append(consensus)

    return signals


def _build_signal(event: dict[str, Any], signal_type: str, reason: str) -> dict[str, Any]:
    return {
        "signal_key": _signal_key(event, signal_type),
        "wallet": event.get("wallet") or "",
        "market_id": event.get("market_id") or "",
        "market_title": event.get("market_title") or "",
        "asset": event.get("asset") or "OTHER",
        "side": event.get("side") or "unknown",
        "signal_type": signal_type,
        "timestamp": float(event.get("timestamp") or 0.0),
        "action": event.get("action") or "",
        "price": float(event.get("price") or 0.0),
        "shares": float(event.get("shares") or 0.0),
        "amount": float(event.get("amount") or 0.0),
        "tx_hash": event.get("tx_hash") or "",
        "reason": reason,
        "raw": event.get("raw") if isinstance(event.get("raw"), dict) else event,
    }


def persist_signals(
    signals: list[dict[str, Any]],
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    inserted = 0
    skipped = 0
    with closing_connection(db_path) as conn:
        for signal in signals:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO trader_signals (
                    signal_key, wallet, market_id, market_title, asset, side,
                    signal_type, timestamp, action, price, shares, amount,
                    tx_hash, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal["signal_key"],
                    signal["wallet"],
                    signal["market_id"],
                    signal["market_title"],
                    signal["asset"],
                    signal["side"],
                    signal["signal_type"],
                    float(signal["timestamp"]),
                    signal["action"],
                    float(signal["price"]),
                    float(signal["shares"]),
                    float(signal["amount"]),
                    signal["tx_hash"],
                    json.dumps(signal, sort_keys=True),
                    _utc_now(),
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1
    return _with_flags(
        {
            "signals_received": len(signals),
            "signals_inserted": inserted,
            "signals_skipped": skipped,
        }
    )


def score_signal_row(signal: dict[str, Any]) -> float:
    signal_type = str(signal.get("signal_type") or "")
    base = SIGNAL_TYPE_WEIGHTS.get(signal_type, 50.0)
    price = float(signal.get("price") or 0.0)
    amount = float(signal.get("amount") or 0.0)
    bonus = 0.0
    if signal_type == "early_entry":
        bonus = max(0.0, (0.55 - price) * 40.0)
    elif signal_type == "conviction":
        bonus = min(20.0, amount / 25.0)
    elif signal_type == "consensus":
        bonus = min(15.0, float(signal.get("consensus_wallet_count") or 2) * 3.0)
    elif signal_type == "contrarian":
        bonus = 10.0 if price >= 0.75 or price <= 0.25 else 5.0
    elif signal_type == "exit":
        bonus = 5.0 if price > 0 else 0.0
    return round(min(100.0, max(0.0, base + bonus)), 4)


def score_trader_signals(*, db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    scored = 0
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT signal_key, signal_type, price, amount, raw_json
            FROM trader_signals
            WHERE score IS NULL
            ORDER BY timestamp ASC, signal_key ASC
            """
        ).fetchall()
        for row in rows:
            payload = dict(row)
            raw = json.loads(str(row["raw_json"] or "{}"))
            if isinstance(raw, dict):
                payload.update(raw)
            score = score_signal_row(payload)
            conn.execute("UPDATE trader_signals SET score=? WHERE signal_key=?", (score, row["signal_key"]))
            scored += 1
        total = int(conn.execute("SELECT COUNT(*) FROM trader_signals WHERE score IS NOT NULL").fetchone()[0])
    return _with_flags({"signals_scored": scored, "signals_with_scores": total})


def update_signal_performance(
    outcomes: list[dict[str, Any]],
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    inserted = 0
    skipped = 0
    with closing_connection(db_path) as conn:
        for row in outcomes:
            signal_key = str(row.get("signal_key") or "")
            outcome_key = str(row.get("outcome_key") or signal_key)
            if not signal_key:
                skipped += 1
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO signal_performance (
                    signal_key, outcome_key, wallet, market_id, signal_type,
                    entry_price, exit_price, pnl, roi, outcome, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_key,
                    outcome_key,
                    _normalize_wallet(str(row.get("wallet") or "")),
                    str(row.get("market_id") or ""),
                    str(row.get("signal_type") or ""),
                    _optional_float(row.get("entry_price")),
                    _optional_float(row.get("exit_price")),
                    _optional_float(row.get("pnl")),
                    _optional_float(row.get("roi")),
                    str(row.get("outcome") or "unknown"),
                    _utc_now(),
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1
    return _with_flags(
        {
            "outcomes_received": len(outcomes),
            "performance_inserted": inserted,
            "performance_skipped": skipped,
        }
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def generate_trader_signal_recommendations(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    limit: int = 20,
) -> dict[str, Any]:
    from src.analysis.trader_signal_gates import (
        apply_gate_to_recommendation,
        load_signal_family_stats,
        split_recommendations_by_gate,
        trader_signal_gates_report,
    )
    from src.analysis.trader_signal_validation import init_trader_signal_validation_db

    init_trader_signal_db(db_path)
    score_trader_signals(db_path=db_path)
    family_stats = load_signal_family_stats(db_path=db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT signal_key, wallet, market_id, market_title, asset, side,
                   signal_type, score, action, timestamp
            FROM trader_signals
            WHERE score IS NOT NULL
            ORDER BY score DESC, timestamp DESC
            """
        ).fetchall()

    draft: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        score = float(row["score"] or 0.0)
        signal_type = str(row["signal_type"])
        recommendation_type = _recommendation_for_signal(signal_type, score, str(row["action"]))
        if recommendation_type is None:
            continue
        key = (str(row["wallet"]), str(row["market_id"]))
        grouped.setdefault(key, []).append(recommendation_type)
        draft.append(
            apply_gate_to_recommendation(
                {
                    "recommendation_key": f"{row['signal_key']}:{recommendation_type}",
                    "wallet": row["wallet"],
                    "market_id": row["market_id"],
                    "market_title": row["market_title"],
                    "asset": row["asset"],
                    "side": row["side"],
                    "recommendation_type": recommendation_type,
                    "signal_type": signal_type,
                    "score": score,
                    "reason": _recommendation_reason(recommendation_type, signal_type, score),
                    "conflict": False,
                },
                family_stats,
            )
        )

    conflict_keys = {key for key, types in grouped.items() if "paper_entry" in types and "paper_exit" in types}
    recommendations: list[dict[str, Any]] = []
    for item in draft:
        if item.get("recommendation_type") == "blocked":
            recommendations.append(item)
            continue
        key = (item["wallet"], item["market_id"])
        if key in conflict_keys:
            item = {
                **item,
                "recommendation_type": "avoid",
                "recommendation_key": f"{item['recommendation_key']}:conflict",
                "reason": "conflicting paper entry and exit signals for the same wallet and market",
                "conflict": True,
            }
        recommendations.append(item)

    recommendations.sort(key=lambda row: (-float(row["score"]), row["recommendation_key"]))
    recommendations = recommendations[: max(int(limit), 0)]
    split = split_recommendations_by_gate(recommendations)

    inserted = 0
    with closing_connection(db_path) as conn:
        conn.execute("DELETE FROM trader_signal_recommendations")
        for item in recommendations:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO trader_signal_recommendations (
                    recommendation_key, wallet, market_id, recommendation_type,
                    signal_type, score, reason, conflict, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["recommendation_key"],
                    item["wallet"],
                    item["market_id"],
                    item["recommendation_type"],
                    item["signal_type"],
                    float(item["score"]),
                    item["reason"],
                    1 if item["conflict"] else 0,
                    _utc_now(),
                ),
            )
            inserted += cursor.rowcount

    return _with_flags(
        {
            "recommendations": recommendations,
            "recommendation_count": len(recommendations),
            "recommendations_persisted": inserted,
            "conflicts": len(conflict_keys),
            "promoted_recommendations": split["promoted_recommendations"],
            "blocked_recommendations": split["blocked_recommendations"],
            "gate_summary": trader_signal_gates_report(db_path=db_path)["gate_summary"],
        }
    )


def _recommendation_for_signal(signal_type: str, score: float, action: str) -> str | None:
    if signal_type == "exit" or action == "sell":
        return "paper_exit" if score >= 55 else "watch"
    if signal_type in {"early_entry", "conviction", "consensus"} and score >= 70:
        return "paper_entry"
    if signal_type == "contrarian" and score < 60:
        return "avoid"
    if score >= 60:
        return "watch"
    if signal_type == "contrarian":
        return "avoid"
    return "watch"


def _recommendation_reason(recommendation_type: str, signal_type: str, score: float) -> str:
    return f"{recommendation_type} from {signal_type} signal with score {score:.2f}"


def trader_signal_leaderboard(*, db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB, limit: int = 20) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    score_trader_signals(db_path=db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                wallet,
                COUNT(*) AS signal_count,
                AVG(score) AS avg_score,
                MAX(score) AS max_score,
                SUM(CASE WHEN signal_type='conviction' THEN 1 ELSE 0 END) AS conviction_count,
                SUM(CASE WHEN signal_type='early_entry' THEN 1 ELSE 0 END) AS early_entry_count
            FROM trader_signals
            WHERE score IS NOT NULL
            GROUP BY wallet
            ORDER BY avg_score DESC, signal_count DESC, wallet ASC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    leaderboard = [
        {
            "wallet": row["wallet"],
            "signal_count": int(row["signal_count"]),
            "avg_score": round(float(row["avg_score"] or 0.0), 4),
            "max_score": round(float(row["max_score"] or 0.0), 4),
            "conviction_count": int(row["conviction_count"]),
            "early_entry_count": int(row["early_entry_count"]),
        }
        for row in rows
    ]
    return _with_flags({"leaderboard": leaderboard, "count": len(leaderboard)})


def trader_signal_report(
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    include_recommendations: bool = False,
    recommendation_limit: int = 20,
    include_paper_bridge_summary: bool = False,
) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    score_trader_signals(db_path=db_path)
    with closing_connection(db_path) as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_signals,
                COUNT(DISTINCT wallet) AS wallets,
                COUNT(DISTINCT market_id) AS markets,
                AVG(score) AS avg_score
            FROM trader_signals
            """
        ).fetchone()
        by_type_rows = conn.execute(
            """
            SELECT signal_type, COUNT(*) AS count, AVG(score) AS avg_score
            FROM trader_signals
            GROUP BY signal_type
            ORDER BY count DESC
            """
        ).fetchall()
        performance = conn.execute(
            """
            SELECT outcome, COUNT(*) AS count, AVG(roi) AS avg_roi
            FROM signal_performance
            GROUP BY outcome
            ORDER BY count DESC
            """
        ).fetchall()

    report = _with_flags(
        {
            "summary": {
                "total_signals": int(totals["total_signals"] or 0),
                "wallets": int(totals["wallets"] or 0),
                "markets": int(totals["markets"] or 0),
                "avg_score": round(float(totals["avg_score"] or 0.0), 4),
            },
            "by_signal_type": [
                {
                    "signal_type": row["signal_type"],
                    "count": int(row["count"]),
                    "avg_score": round(float(row["avg_score"] or 0.0), 4),
                }
                for row in by_type_rows
            ],
            "performance": [
                {
                    "outcome": row["outcome"],
                    "count": int(row["count"]),
                    "avg_roi": round(float(row["avg_roi"] or 0.0), 4) if row["avg_roi"] is not None else None,
                }
                for row in performance
            ],
        }
    )
    if include_recommendations:
        recommendation_result = generate_trader_signal_recommendations(
            db_path=db_path,
            limit=recommendation_limit,
        )
        report["recommendations"] = recommendation_result["recommendations"]
        report["promoted_recommendations"] = recommendation_result["promoted_recommendations"]
        report["blocked_recommendations"] = recommendation_result["blocked_recommendations"]
        report["gate_summary"] = recommendation_result["gate_summary"]
    else:
        from src.analysis.trader_signal_gates import trader_signal_gates_report

        report["gate_summary"] = trader_signal_gates_report(db_path=db_path)["gate_summary"]
    if include_paper_bridge_summary:
        from src.analysis.trader_signal_paper_bridge import trader_signal_paper_bridge_summary

        report["paper_bridge_summary"] = trader_signal_paper_bridge_summary(db_path=db_path)
    return report


def run_trader_signal_cycle(
    *,
    activity_path: str | Path | None = None,
    outcomes_path: str | Path | None = None,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
    recommendation_limit: int = 20,
) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    events: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    if activity_path:
        events = load_activity_json(activity_path)
    if outcomes_path:
        outcomes = load_outcomes_json(outcomes_path)

    generated = generate_signals_from_activity(events)
    persisted = persist_signals(generated, db_path=db_path)
    scored = score_trader_signals(db_path=db_path)
    performance = update_signal_performance(outcomes, db_path=db_path)
    recommendations = generate_trader_signal_recommendations(db_path=db_path, limit=recommendation_limit)
    metadata = {
        "activity_event_count": len(events),
        "outcome_count": len(outcomes),
        "leaderboard_count": trader_signal_leaderboard(db_path=db_path, limit=5)["count"],
    }
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trader_signal_cycles (
                cycle_timestamp, activity_path, outcomes_path,
                signals_generated, signals_persisted, signals_scored,
                performance_updated, recommendations_generated, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                str(activity_path) if activity_path else None,
                str(outcomes_path) if outcomes_path else None,
                len(generated),
                int(persisted["signals_inserted"]),
                int(scored["signals_scored"]),
                int(performance["performance_inserted"]),
                int(recommendations["recommendation_count"]),
                json.dumps(metadata, sort_keys=True),
            ),
        )

    return _with_flags(
        {
            "cycle_timestamp": _utc_now(),
            "activity_path": str(activity_path) if activity_path else None,
            "outcomes_path": str(outcomes_path) if outcomes_path else None,
            "signals_generated": len(generated),
            "persisted": persisted,
            "scored": scored,
            "performance": performance,
            "recommendations": recommendations,
            "metadata": metadata,
        }
    )


def trader_signal_health(*, db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB) -> dict[str, Any]:
    init_trader_signal_db(db_path)
    with closing_connection(db_path) as conn:
        signal_count = int(conn.execute("SELECT COUNT(*) FROM trader_signals").fetchone()[0])
        scored_count = int(conn.execute("SELECT COUNT(*) FROM trader_signals WHERE score IS NOT NULL").fetchone()[0])
        recommendation_count = int(conn.execute("SELECT COUNT(*) FROM trader_signal_recommendations").fetchone()[0])
        performance_count = int(conn.execute("SELECT COUNT(*) FROM signal_performance").fetchone()[0])
        last_cycle = conn.execute(
            "SELECT cycle_timestamp, signals_generated, recommendations_generated FROM trader_signal_cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()

    status = "healthy" if signal_count > 0 and scored_count == signal_count else "idle"
    if signal_count == 0:
        status = "empty"

    return _with_flags(
        {
            "status": status,
            "db_path": str(db_path),
            "signal_count": signal_count,
            "scored_count": scored_count,
            "recommendation_count": recommendation_count,
            "performance_count": performance_count,
            "last_cycle": {
                "cycle_timestamp": last_cycle["cycle_timestamp"],
                "signals_generated": int(last_cycle["signals_generated"]),
                "recommendations_generated": int(last_cycle["recommendations_generated"]),
            }
            if last_cycle
            else None,
        }
    )


def generate_and_persist_signals_from_activity_path(
    activity_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
) -> dict[str, Any]:
    events = load_activity_json(activity_path)
    generated = generate_signals_from_activity(events)
    persisted = persist_signals(generated, db_path=db_path)
    return _with_flags(
        {
            "activity_path": str(activity_path),
            "events_loaded": len(events),
            "signals": generated,
            "signals_generated": len(generated),
            **{key: persisted[key] for key in ("signals_inserted", "signals_skipped", "signals_received")},
        }
    )
