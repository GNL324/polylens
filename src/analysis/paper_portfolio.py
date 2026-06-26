from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.paper_trading_engine import DEFAULT_STARTING_BANKROLL
from src.sqlite_utils import closing_connection


ANOMALOUS_STATUS = "legacy_anomalous"
VALID_POSITION_STATUSES = ("open", "closed")


def migrate_paper_portfolio(
    db_path: str | Path,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> dict[str, int]:
    """Add inspectable ledger metadata and quarantine impossible legacy rows."""
    with closing_connection(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(paper_positions)").fetchall()}
        for name, definition in (
            ("signal_family", "TEXT"),
            ("source_wallet", "TEXT"),
            ("reason", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {name} {definition}")

        rows = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id").fetchall()
        anomalous: set[int] = set()
        reserved = 0.0
        for row in rows:
            position_id = int(row["paper_position_id"])
            status = str(row["status"] or "").lower()
            notional = _number(row["notional"])
            entry = _number(row["entry_price"])
            exit_price = _number(row["exit_price"])
            pnl = _number(row["realized_pnl"])
            invalid_price = entry <= 0 or entry > 1 or (row["exit_price"] is not None and (exit_price < 0 or exit_price > 1))
            invalid_pnl = status != "open" and abs(pnl) > notional + 0.01
            invalid_notional = notional <= 0 or notional > starting_bankroll
            if invalid_price or invalid_pnl or invalid_notional:
                anomalous.add(position_id)
                continue
            if status == "open":
                reserved += notional
                if reserved > starting_bankroll + 0.01:
                    anomalous.add(position_id)
        for position_id in anomalous:
            conn.execute(
                "UPDATE paper_positions SET status=?, reason=COALESCE(reason, ?) WHERE paper_position_id=?",
                (ANOMALOUS_STATUS, "excluded from canonical $100 portfolio", position_id),
            )
    return {"legacy_anomalous": len(anomalous)}


def portfolio_report(
    db_path: str | Path,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> dict[str, Any]:
    migrate_paper_portfolio(db_path, starting_bankroll=starting_bankroll)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id").fetchall()
    normal = [row for row in rows if str(row["status"] or "").lower() in VALID_POSITION_STATUSES]
    open_rows = [row for row in normal if str(row["status"] or "").lower() == "open"]
    closed_rows = [row for row in normal if str(row["status"] or "").lower() == "closed"]
    realized = round(sum(_number(row["realized_pnl"]) for row in closed_rows), 6)
    open_cost = round(sum(_number(row["notional"]) for row in open_rows), 6)
    open_value = round(sum(_number(row["shares"]) * _mark_price(row) for row in open_rows), 6)
    unrealized = round(open_value - open_cost, 6)
    cash = round(starting_bankroll + realized - open_cost, 6)
    cash = max(0.0, cash)
    equity = round(cash + open_value, 6)
    total_pnl = round(equity - starting_bankroll, 6)
    wins = sum(1 for row in closed_rows if _number(row["realized_pnl"]) > 0)
    return {
        "starting_bankroll": round(float(starting_bankroll), 6),
        "cash_balance": cash,
        "available_cash": cash,
        "open_position_value": open_value,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_equity": equity,
        "equity": equity,
        "total_pnl": total_pnl,
        "roi_pct": round((total_pnl / starting_bankroll) * 100.0, 6) if starting_bankroll else 0.0,
        "roi": round(total_pnl / starting_bankroll, 6) if starting_bankroll else 0.0,
        "trade_count": len(normal),
        "total_trades": len(normal),
        "open_positions": len(open_rows),
        "closed_positions": len(closed_rows),
        "closed_trades": len(closed_rows),
        "win_rate": round(wins / len(closed_rows), 6) if closed_rows else 0.0,
        "legacy_anomalous_count": sum(1 for row in rows if str(row["status"] or "").lower() == ANOMALOUS_STATUS),
        "by_strategy": _by_strategy(normal),
    }


def portfolio_trade_log(db_path: str | Path, *, limit: int = 25) -> list[dict[str, Any]]:
    migrate_paper_portfolio(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id DESC LIMIT ?", (max(int(limit), 0),)).fetchall()
    return [_trade_row(row) for row in rows]


def _trade_row(row: Any) -> dict[str, Any]:
    status = str(row["status"] or "").lower()
    is_open = status == "open"
    return {
        "id": str(row["paper_position_id"]),
        "timestamp": str(row["entry_timestamp"] or ""),
        "market_title": str(row["title"] or row["market_id"] or "unknown"),
        "market_id": str(row["market_id"] or ""),
        "outcome": str(row["side"] or ""),
        "side": str(row["side"] or ""),
        "strategy": str(row["strategy"] or "unknown"),
        "signal_family": str(row["signal_family"] or row["strategy"] or "unknown"),
        "entry_price": _number(row["entry_price"]),
        "exit_price": _number(row["exit_price"]) if row["exit_price"] is not None else None,
        "stake": _number(row["notional"]),
        "notional": _number(row["notional"]),
        "shares": _number(row["shares"]),
        "status": status,
        "realized_pnl": _number(row["realized_pnl"]),
        "unrealized_pnl": _number(row["unrealized_pnl"]) if is_open else 0.0,
        "source_wallet": row["source_wallet"],
        "reason": row["reason"],
        "opened_at": str(row["entry_timestamp"] or ""),
        "closed_at": str(row["exit_timestamp"] or "") or None,
        "pnl": _number(row["unrealized_pnl"]) if is_open else _number(row["realized_pnl"]),
    }


def _by_strategy(rows: list[Any]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["strategy"] or "unknown")
        bucket = buckets.setdefault(name, {"strategy": name, "trade_count": 0, "open_positions": 0, "closed_positions": 0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "win_rate": 0.0})
        bucket["trade_count"] += 1
        if str(row["status"] or "").lower() == "open":
            bucket["open_positions"] += 1
            bucket["unrealized_pnl"] += _number(row["unrealized_pnl"])
        else:
            bucket["closed_positions"] += 1
            bucket["realized_pnl"] += _number(row["realized_pnl"])
    for bucket in buckets.values():
        closed = bucket["closed_positions"]
        wins = sum(1 for row in rows if str(row["strategy"] or "unknown") == bucket["strategy"] and str(row["status"] or "").lower() == "closed" and _number(row["realized_pnl"]) > 0)
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 6)
        bucket["unrealized_pnl"] = round(bucket["unrealized_pnl"], 6)
        bucket["win_rate"] = round(wins / closed, 6) if closed else 0.0
    return dict(sorted(buckets.items()))


def _mark_price(row: Any) -> float:
    return min(1.0, max(0.0, _number(row["current_price"])))


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0



_legacy_portfolio_trade_log = portfolio_trade_log


def portfolio_trade_log(db_path: str | Path, *, limit: int = 25) -> list[dict[str, Any]]:
    import json

    rows = _legacy_portfolio_trade_log(db_path, limit=limit)
    with closing_connection(db_path) as conn:
        blocked = conn.execute(
            """
            SELECT o.*, r.run_timestamp
            FROM paper_orders o
            LEFT JOIN paper_positions p ON p.order_id = o.id
            LEFT JOIN paper_runs r ON r.id = o.run_id
            WHERE o.status='blocked' AND p.paper_position_id IS NULL
            ORDER BY o.id DESC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    for row in blocked:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        rows.append(
            {
                "id": f"blocked-{row['id']}",
                "timestamp": str(row["run_timestamp"] or ""),
                "market_title": str(row["title"] or row["market_id"] or "unknown"),
                "market_id": str(row["market_id"] or ""),
                "outcome": str(row["side"] or ""),
                "side": str(row["side"] or ""),
                "strategy": str(row["strategy"] or "unknown"),
                "signal_family": str(raw.get("signal_family") or raw.get("signal_type") or row["strategy"] or "unknown"),
                "entry_price": _number(row["simulated_price"]),
                "exit_price": None,
                "stake": 0.0,
                "notional": 0.0,
                "shares": 0.0,
                "status": "blocked",
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "source_wallet": raw.get("wallet") or raw.get("trader_address"),
                "reason": raw.get("blocked_reason") or "paper portfolio limit",
                "opened_at": str(row["run_timestamp"] or ""),
                "closed_at": None,
                "pnl": 0.0,
            }
        )
    rows.sort(key=lambda item: str(item.get("timestamp") or item.get("opened_at") or ""), reverse=True)
    return rows[: max(int(limit), 0)]
