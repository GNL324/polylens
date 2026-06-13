from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, init_paper_trading_db, record_equity_snapshot
from src.analysis.settlement_sources import resolve_opportunity_outcome
from src.sqlite_utils import closing_connection


def run_paper_settlement(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    run_id: int = 0,
) -> dict[str, Any]:
    _init_settlement_audit(db_path)
    checked = 0
    settled = 0
    unresolved = 0
    reasons: dict[str, int] = {}
    details: list[dict[str, Any]] = []
    with closing_connection(db_path) as conn:
        positions = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY paper_position_id").fetchall()
        checked = len(positions)
        for position in positions:
            if _already_settled(conn, int(position["paper_position_id"])):
                _mark_unresolved(conn, run_id, position, "already_settled")
                _count(reasons, "already_settled")
                unresolved += 1
                details.append(_detail(position, "already_settled", False))
                continue
            order = conn.execute("SELECT * FROM paper_orders WHERE id=?", (position["order_id"],)).fetchone()
            raw = _loads(order["raw_json"]) if order else {}
            evaluation = evaluate_position_for_settlement(position, raw)
            if evaluation["resolved"]:
                pnl = (_float(evaluation["exit_price"]) - _float(position["entry_price"])) * _float(position["shares"])
                roi = _ratio(pnl, _float(position["notional"]))
                timestamp = _utc_now()
                conn.execute(
                    """
                    UPDATE paper_positions
                    SET status='closed', exit_timestamp=?, exit_price=?, realized_pnl=?, unrealized_pnl=0, roi=?
                    WHERE paper_position_id=? AND status='open'
                    """,
                    (timestamp, evaluation["exit_price"], round(pnl, 6), round(roi, 6), position["paper_position_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, position["paper_position_id"], timestamp, evaluation["exit_price"], round(pnl, 6), round(roi, 6), evaluation["reason"]),
                )
                _record_evaluation(conn, run_id, position, "settled", evaluation["reason"], evaluation["exit_price"])
                settled += 1
                details.append(_detail(position, evaluation["reason"], True, evaluation["exit_price"], pnl, roi))
            else:
                reason = str(evaluation["reason"])
                _mark_unresolved(conn, run_id, position, reason)
                _count(reasons, reason)
                unresolved += 1
                details.append(_detail(position, reason, False))
    if settled:
        record_equity_snapshot(db_path, run_id=run_id)
    return {
        "open_positions_checked": checked,
        "positions_settled": settled,
        "positions_unresolved": unresolved,
        "reasons": dict(sorted(reasons.items())),
        "details": details,
    }


def paper_settlement_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> dict[str, Any]:
    _init_settlement_audit(db_path)
    with closing_connection(db_path) as conn:
        open_count = int(conn.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0])
        settled_count = int(conn.execute("SELECT COUNT(*) FROM paper_settlements").fetchone()[0])
        unresolved_rows = conn.execute(
            """
            SELECT reason, COUNT(*) AS count
            FROM paper_settlement_evaluations
            WHERE status='unresolved'
            GROUP BY reason
            ORDER BY reason
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT *
            FROM paper_settlement_evaluations
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()
    return {
        "open_positions": open_count,
        "settlements_recorded": settled_count,
        "unresolved_reasons": {row["reason"]: row["count"] for row in unresolved_rows},
        "recent_evaluations": [_row_dict(row) for row in recent],
    }


def evaluate_position_for_settlement(position: Any, raw: dict[str, Any]) -> dict[str, Any]:
    sourced = resolve_opportunity_outcome(str(position["opportunity_id"]))
    if sourced.get("resolved"):
        return {
            "resolved": True,
            "exit_price": _bounded_price(_float(sourced.get("settlement_price"))),
            "reason": f"settlement_source:{sourced.get('source')}",
        }
    status = str(_first(raw, "market_status", "status", "state") or "").strip().lower()
    explicit = _first_float(raw, "settlement_price", "exit_price")
    if explicit is not None:
        return {"resolved": True, "exit_price": _bounded_price(explicit), "reason": "explicit_settlement_price"}
    outcome = _first(raw, "outcome", "winner", "resolved_outcome")
    if outcome not in (None, ""):
        won = _outcome_matches(position["side"], str(outcome))
        return {"resolved": True, "exit_price": 1.0 if won else 0.0, "reason": "resolved_won" if won else "resolved_lost"}
    if status in {"open", "active", "trading"}:
        mark = _first_float(raw, "current_market_price", "current_price", "mark_price")
        return {"resolved": False, "exit_price": mark, "reason": "market_still_open"}
    if status in {"closed", "resolved", "settled"}:
        mark = _first_float(raw, "current_market_price", "current_price", "mark_price")
        if mark is not None:
            return {"resolved": True, "exit_price": _bounded_price(mark), "reason": "closed_with_market_price"}
        return {"resolved": False, "exit_price": None, "reason": "missing_outcome"}
    if status in {"expired", "cancelled", "canceled"}:
        return {"resolved": False, "exit_price": None, "reason": "expired_unknown"}
    mark = _first_float(raw, "current_market_price", "current_price", "mark_price")
    if mark is not None:
        return {"resolved": False, "exit_price": mark, "reason": "market_still_open"}
    return {"resolved": False, "exit_price": None, "reason": "missing_outcome"}


def _init_settlement_audit(db_path: str | Path) -> None:
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_settlement_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                paper_position_id INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                exit_price REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_settlement_eval_position ON paper_settlement_evaluations(paper_position_id)")


def _record_evaluation(conn: Any, run_id: int, position: Any, status: str, reason: str, exit_price: float | None = None) -> None:
    conn.execute(
        """
        INSERT INTO paper_settlement_evaluations (run_id, paper_position_id, evaluated_at, status, reason, exit_price)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, position["paper_position_id"], _utc_now(), status, reason, exit_price),
    )


def _mark_unresolved(conn: Any, run_id: int, position: Any, reason: str) -> None:
    _record_evaluation(conn, run_id, position, "unresolved", reason, None)


def _already_settled(conn: Any, paper_position_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM paper_settlements WHERE paper_position_id=? LIMIT 1", (paper_position_id,)).fetchone()
    return row is not None


def _detail(position: Any, reason: str, settled: bool, exit_price: float | None = None, pnl: float | None = None, roi: float | None = None) -> dict[str, Any]:
    return {
        "paper_position_id": position["paper_position_id"],
        "opportunity_id": position["opportunity_id"],
        "strategy": position["strategy"],
        "asset": position["asset"],
        "status": "settled" if settled else "unresolved",
        "reason": reason,
        "exit_price": None if exit_price is None else round(float(exit_price), 6),
        "pnl": None if pnl is None else round(float(pnl), 6),
        "roi": None if roi is None else round(float(roi), 6),
    }


def _outcome_matches(side: Any, outcome: str) -> bool:
    left = str(side or "").strip().lower()
    right = outcome.strip().lower()
    aliases = {
        "up": {"up", "yes", "true", "1", "win", "won"},
        "down": {"down", "no", "false", "0", "loss", "lost"},
        "yes": {"yes", "true", "1", "up", "win", "won"},
        "no": {"no", "false", "0", "down", "loss", "lost"},
    }
    return right in aliases.get(left, {left})


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


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


def _bounded_price(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _count(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
