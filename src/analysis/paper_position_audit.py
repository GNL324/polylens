from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.opportunity_feed import get_paper_trading_opportunities
from src.analysis.paper_trading_engine import DEFAULT_MAX_OPEN_POSITIONS, DEFAULT_PAPER_TRADING_DB, init_paper_trading_db
from src.sqlite_utils import closing_connection


def build_paper_position_audit(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    opportunities: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    now = now or datetime.now(timezone.utc)
    positions = _load_rows(db_path, "SELECT * FROM paper_positions ORDER BY paper_position_id")
    orders = {row["id"]: row for row in _load_rows(db_path, "SELECT * FROM paper_orders ORDER BY id")}
    runs = _load_rows(db_path, "SELECT * FROM paper_runs ORDER BY id")
    open_positions = [row for row in positions if row["status"] == "open"]
    closed_positions = [row for row in positions if row["status"] == "closed"]
    opportunities = opportunities if opportunities is not None else get_paper_trading_opportunities()
    existing_ids = {str(row["opportunity_id"]) for row in positions}
    duplicate_opportunities = [item for item in opportunities if str(item.get("opportunity_id") or item.get("id")) in existing_ids]
    rejected = _rejected_opportunities(opportunities, positions)
    exit_rows = [_exit_audit(row, orders.get(row["order_id"]), now) for row in open_positions]
    root_causes = _root_causes(exit_rows, duplicate_opportunities, rejected, runs)
    return {
        "open_positions": len(open_positions),
        "positions": exit_rows,
        "positions_eligible_for_exit": sum(1 for row in exit_rows if row["eligible_for_exit"]),
        "duplicate_opportunities": len(duplicate_opportunities),
        "rejected_opportunities": len(rejected),
        "rejection_reasons": _reason_counts(row["reason"] for row in rejected),
        "opportunity_flow": {
            "opportunities_discovered": len(opportunities),
            "opportunities_rejected": len(rejected),
            "duplicate_opportunities": len(duplicate_opportunities),
            "new_opportunities": max(0, len(opportunities) - len(duplicate_opportunities)),
        },
        "service_behavior": {
            "runs_completed": len(runs),
            "runs_with_new_entries": sum(1 for row in runs if int(row["positions_opened"] or 0) > 0),
            "runs_with_exits": sum(1 for row in runs if int(row["positions_settled"] or 0) > 0),
            "runs_doing_nothing": sum(1 for row in runs if int(row["positions_opened"] or 0) == 0 and int(row["positions_settled"] or 0) == 0),
            "latest_run": dict(runs[-1]) if runs else None,
        },
        "closed_positions": len(closed_positions),
        "root_causes": root_causes,
    }


def _exit_audit(position: Any, order: Any | None, now: datetime) -> dict[str, Any]:
    raw = _loads(order["raw_json"]) if order is not None else {}
    exit_price = _first_float(raw, "settlement_price", "exit_price", "target_price")
    reasons: list[str] = []
    if exit_price is None or exit_price <= 0:
        reasons.append("missing close-price source")
    if not raw:
        reasons.append("missing market data")
    if not _has_outcome(raw):
        reasons.append("unresolved outcome")
    eligible = not reasons
    entry_time = _parse_time(position["entry_timestamp"])
    return {
        "paper_position_id": position["paper_position_id"],
        "opportunity_id": position["opportunity_id"],
        "age_minutes": round((now - entry_time).total_seconds() / 60.0, 3) if entry_time else None,
        "strategy": position["strategy"],
        "asset": position["asset"],
        "market_id": position["market_id"],
        "title": position["title"],
        "entry_timestamp": position["entry_timestamp"],
        "entry_price": position["entry_price"],
        "current_status": position["status"],
        "eligible_for_exit": eligible,
        "exit_price_available": exit_price is not None and exit_price > 0,
        "reasons_not_settling": reasons,
    }


def _rejected_opportunities(opportunities: list[dict[str, Any]], positions: list[Any]) -> list[dict[str, str]]:
    open_count = sum(1 for row in positions if row["status"] == "open")
    existing_ids = {str(row["opportunity_id"]) for row in positions}
    rejected: list[dict[str, str]] = []
    for item in opportunities:
        opportunity_id = str(item.get("opportunity_id") or item.get("id") or "")
        if opportunity_id in existing_ids:
            rejected.append({"opportunity_id": opportunity_id, "reason": "duplicate opportunity suppression"})
        elif open_count >= DEFAULT_MAX_OPEN_POSITIONS:
            rejected.append({"opportunity_id": opportunity_id, "reason": "max open positions reached"})
        elif not item.get("eligible_for_paper_trading", True):
            rejected.append({"opportunity_id": opportunity_id, "reason": "not eligible for paper trading"})
    return rejected


def _root_causes(exit_rows: list[dict[str, Any]], duplicates: list[dict[str, Any]], rejected: list[dict[str, str]], runs: list[Any]) -> list[str]:
    causes: list[str] = []
    if duplicates:
        causes.append("duplicate opportunity suppression")
    if any("missing close-price source" in row["reasons_not_settling"] for row in exit_rows):
        causes.append("missing close-price source")
    if any("unresolved outcome" in row["reasons_not_settling"] for row in exit_rows):
        causes.append("unresolved outcome")
    if exit_rows and not any(row["eligible_for_exit"] for row in exit_rows):
        causes.append("no settlement evaluator")
    if any(row["reason"] == "max open positions reached" for row in rejected):
        causes.append("max open positions reached")
    if runs and all(int(row["positions_opened"] or 0) == 0 and int(row["positions_settled"] or 0) == 0 for row in runs[-5:]):
        causes.append("service loop is idling")
    return causes


def _load_rows(db_path: str | Path, query: str) -> list[Any]:
    with closing_connection(db_path) as conn:
        return conn.execute(query).fetchall()


def _reason_counts(reasons: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = __import__("json").loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _has_outcome(row: dict[str, Any]) -> bool:
    return any(row.get(key) not in (None, "") for key in ("settlement_price", "exit_price", "target_price", "outcome", "resolved_outcome"))
