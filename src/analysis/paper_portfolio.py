from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from src.analysis.paper_trading_engine import DEFAULT_STARTING_BANKROLL
from src.sqlite_utils import closing_connection


ANOMALOUS_STATUS = "legacy_anomalous"
OPEN_STATUSES = ("pending", "open", "partially_exited")
CLOSED_STATUSES = ("closed", "expired")
VALID_POSITION_STATUSES = (*OPEN_STATUSES, *CLOSED_STATUSES)


def migrate_paper_portfolio(
    db_path: str | Path,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> dict[str, int]:
    """Make the paper ledger forward-compatible and quarantine impossible rows.

    The migration is deliberately additive: historical experiments remain visible as
    ``legacy_anomalous`` records, but cannot affect canonical cash or analytics.
    """
    with closing_connection(db_path) as conn:
        _add_columns(
            conn,
            "paper_positions",
            {
                "signal_family": "TEXT",
                "source_wallet": "TEXT",
                "reason": "TEXT",
                "confidence": "REAL",
                "validation_score": "REAL",
                "market_category": "TEXT",
                "market_liquidity": "REAL",
                "time_to_expiry_seconds": "REAL",
                "created_timestamp": "TEXT",
                "updated_timestamp": "TEXT",
                "pending_timestamp": "TEXT",
                "open_timestamp": "TEXT",
                "partially_exited_timestamp": "TEXT",
                "closed_timestamp": "TEXT",
                "expired_timestamp": "TEXT",
                "blocked_timestamp": "TEXT",
            },
        )
        _add_columns(
            conn,
            "paper_orders",
            {
                "created_timestamp": "TEXT",
                "blocked_reason": "TEXT",
            },
        )
        _add_columns(conn, "paper_equity_curve", {"canonical": "INTEGER NOT NULL DEFAULT 1"})
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_position_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_position_id INTEGER,
                order_id INTEGER,
                timestamp TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                reason TEXT,
                quantity REAL,
                price REAL,
                raw_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(paper_position_id) REFERENCES paper_positions(paper_position_id),
                FOREIGN KEY(order_id) REFERENCES paper_orders(id)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_position_transitions_position
                ON paper_position_transitions(paper_position_id, id);
            CREATE TABLE IF NOT EXISTS paper_portfolio_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
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
            # Fully exited V2 rows have no remaining notional.  Only use a
            # positive legacy basis when checking an implausible realised PnL.
            invalid_pnl = status not in OPEN_STATUSES and notional > 0 and abs(pnl) > notional + 0.01
            invalid_notional = (status in OPEN_STATUSES and notional <= 0) or notional > starting_bankroll
            if invalid_price or invalid_pnl or invalid_notional:
                anomalous.add(position_id)
                continue
            if status in OPEN_STATUSES:
                reserved += notional
                if reserved > starting_bankroll + 0.01:
                    anomalous.add(position_id)
        for position_id in anomalous:
            conn.execute(
                "UPDATE paper_positions SET status=?, reason=COALESCE(reason, ?) WHERE paper_position_id=?",
                (ANOMALOUS_STATUS, "excluded from canonical paper portfolio", position_id),
            )
        # Historic snapshots may have incorporated rows that are now quarantined.
        # Preserve them for forensics but omit them from the canonical V2 curve.
        reset_done = conn.execute(
            "SELECT 1 FROM paper_portfolio_metadata WHERE key='legacy_equity_curve_quarantined_v2'"
        ).fetchone()
        if anomalous and not reset_done:
            conn.execute("UPDATE paper_equity_curve SET canonical=0")
            conn.execute(
                "INSERT INTO paper_portfolio_metadata (key, value) VALUES (?, ?)",
                ("legacy_equity_curve_quarantined_v2", _utc_now()),
            )
    return {"legacy_anomalous": len(anomalous)}


def record_position_transition(
    db_path: str | Path,
    *,
    paper_position_id: int | None,
    order_id: int | None,
    from_status: str | None,
    to_status: str,
    timestamp: str | None = None,
    reason: str | None = None,
    quantity: float | None = None,
    price: float | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_position_transitions
            (paper_position_id, order_id, timestamp, from_status, to_status, reason, quantity, price, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_position_id,
                order_id,
                timestamp or _utc_now(),
                from_status,
                to_status,
                reason,
                quantity,
                price,
                json.dumps(raw or {}, sort_keys=True),
            ),
        )


def position_transitions(db_path: str | Path, paper_position_id: int) -> list[dict[str, Any]]:
    migrate_paper_portfolio(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM paper_position_transitions WHERE paper_position_id=? ORDER BY id",
            (int(paper_position_id),),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def transition_position(
    db_path: str | Path,
    *,
    paper_position_id: int,
    status: str,
    price: float | None = None,
    quantity: float | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Transition a paper position, including deterministic partial-exit accounting."""
    target = str(status or "").lower()
    if target not in (*OPEN_STATUSES, *CLOSED_STATUSES):
        raise ValueError(f"unsupported paper position status: {status}")
    timestamp = timestamp or _utc_now()
    migrate_paper_portfolio(db_path)
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM paper_positions WHERE paper_position_id=?", (int(paper_position_id),)).fetchone()
        if row is None:
            raise ValueError(f"paper position not found: {paper_position_id}")
        previous = str(row["status"] or "").lower()
        shares = _number(row["shares"])
        exit_quantity = shares if quantity is None else min(max(_number(quantity), 0.0), shares)
        exit_price = _number(price if price is not None else row["current_price"])
        realized = _number(row["realized_pnl"])
        notional = _number(row["notional"])
        if target in ("partially_exited", "closed", "expired"):
            if exit_quantity <= 0 or exit_price <= 0:
                raise ValueError("an exit requires positive quantity and price")
            cost_basis = _number(row["entry_price"]) * exit_quantity
            realized = round(realized + ((exit_price * exit_quantity) - cost_basis), 6)
            remaining_shares = round(shares - exit_quantity, 8)
            remaining_notional = round(max(0.0, notional - cost_basis), 6)
            if remaining_shares > 0 and target != "expired":
                target = "partially_exited"
            else:
                target = "expired" if target == "expired" else "closed"
                remaining_shares = 0.0
                remaining_notional = 0.0
            unrealized = round((exit_price - _number(row["entry_price"])) * remaining_shares, 6)
            updates = {
                "status": target,
                "shares": remaining_shares,
                "notional": remaining_notional,
                "current_price": exit_price,
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "exit_price": exit_price if target in CLOSED_STATUSES else row["exit_price"],
                "exit_timestamp": timestamp if target in CLOSED_STATUSES else row["exit_timestamp"],
                "roi": _ratio(realized, _number(row["notional"])) if target in CLOSED_STATUSES else row["roi"],
            }
        else:
            updates = {"status": target, "current_price": exit_price or _number(row["current_price"])}
        stamp_column = f"{target}_timestamp"
        if stamp_column in {"open_timestamp", "pending_timestamp", "partially_exited_timestamp", "closed_timestamp", "expired_timestamp"}:
            updates[stamp_column] = timestamp
        updates["updated_timestamp"] = timestamp
        assignments = ", ".join(f"{key}=?" for key in updates)
        conn.execute(
            f"UPDATE paper_positions SET {assignments} WHERE paper_position_id=?",
            (*updates.values(), int(paper_position_id)),
        )
    record_position_transition(
        db_path,
        paper_position_id=int(paper_position_id),
        order_id=int(row["order_id"]),
        from_status=previous,
        to_status=target,
        timestamp=timestamp,
        reason=reason,
        quantity=exit_quantity if target in (*CLOSED_STATUSES, "partially_exited") else None,
        price=exit_price if target in (*CLOSED_STATUSES, "partially_exited") else None,
    )
    return portfolio_position(db_path, paper_position_id)


def portfolio_position(db_path: str | Path, paper_position_id: int) -> dict[str, Any]:
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM paper_positions WHERE paper_position_id=?", (int(paper_position_id),)).fetchone()
    if row is None:
        raise ValueError(f"paper position not found: {paper_position_id}")
    result = _trade_row(row)
    result["transitions"] = position_transitions(db_path, paper_position_id)
    return result


def portfolio_report(
    db_path: str | Path,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> dict[str, Any]:
    migrate_paper_portfolio(db_path, starting_bankroll=starting_bankroll)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id").fetchall()
        curve = conn.execute("SELECT * FROM paper_equity_curve WHERE COALESCE(canonical, 1)=1 ORDER BY id").fetchall()
    normal = [row for row in rows if str(row["status"] or "").lower() in VALID_POSITION_STATUSES]
    open_rows = [row for row in normal if str(row["status"] or "").lower() in OPEN_STATUSES]
    closed_rows = [row for row in normal if str(row["status"] or "").lower() in CLOSED_STATUSES]
    realized = round(sum(_number(row["realized_pnl"]) for row in closed_rows), 6)
    open_cost = round(sum(_number(row["notional"]) for row in open_rows), 6)
    open_value = round(sum(_number(row["shares"]) * _mark_price(row) for row in open_rows), 6)
    unrealized = round(open_value - open_cost, 6)
    cash = max(0.0, round(starting_bankroll + realized - open_cost, 6))
    equity = round(cash + open_value, 6)
    total_pnl = round(equity - starting_bankroll, 6)
    closed_pnls = [_number(row["realized_pnl"]) for row in closed_rows]
    winners = [value for value in closed_pnls if value > 0]
    losers = [value for value in closed_pnls if value < 0]
    curve_rows = [_row_dict(row) for row in curve]
    equity_values = [starting_bankroll, *[float(row["equity"]) for row in curve_rows]]
    daily_returns = _daily_returns(curve_rows, starting_bankroll)
    total_exposure = round(sum(_number(row["notional"]) for row in open_rows), 6)
    attributions = {
        "strategy": _attribution(normal, "strategy"),
        "signal_family": _attribution(normal, "signal_family"),
        "source_wallet": _attribution(normal, "source_wallet"),
        "market_category": _attribution(normal, "market_category"),
        "asset": _attribution(normal, "asset"),
    }
    return {
        "starting_bankroll": round(float(starting_bankroll), 6),
        "cash_balance": cash,
        "available_cash": cash,
        "open_position_value": open_value,
        "exposure": total_exposure,
        "exposure_pct": round(_ratio(total_exposure, equity), 6),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_equity": equity,
        "equity": equity,
        "total_pnl": total_pnl,
        "cumulative_return": round(_ratio(total_pnl, starting_bankroll), 6),
        "roi_pct": round(_ratio(total_pnl, starting_bankroll) * 100.0, 6),
        "roi": round(_ratio(total_pnl, starting_bankroll), 6),
        "trade_count": len(normal),
        "total_trades": len(normal),
        "open_positions": len(open_rows),
        "closed_positions": len(closed_rows),
        "closed_trades": len(closed_rows),
        "win_rate": round(_ratio(len(winners), len(closed_rows)), 6),
        "expectancy": round(mean(closed_pnls), 6) if closed_pnls else 0.0,
        "profit_factor": round(_ratio(sum(winners), abs(sum(losers))), 6) if losers else (round(sum(winners), 6) if winners else 0.0),
        "average_winner": round(mean(winners), 6) if winners else 0.0,
        "average_loser": round(mean(losers), 6) if losers else 0.0,
        "average_holding_time_seconds": round(_average_holding_seconds(closed_rows), 6),
        "daily_returns": daily_returns,
        "today_return": _period_return(curve_rows, days=1, starting_bankroll=starting_bankroll),
        "weekly_return": _period_return(curve_rows, days=7, starting_bankroll=starting_bankroll),
        "monthly_return": _period_return(curve_rows, days=30, starting_bankroll=starting_bankroll),
        "drawdown": round(equity - max(equity_values), 6),
        "drawdown_pct": round(_ratio(equity - max(equity_values), max(equity_values)), 6),
        "max_drawdown": round(_max_drawdown(equity_values), 6),
        "max_drawdown_pct": round(_max_drawdown_pct(equity_values), 6),
        "sharpe_ratio": round(_sharpe(daily_returns), 6),
        "equity_curve": curve_rows,
        "legacy_anomalous_count": sum(1 for row in rows if str(row["status"] or "").lower() == ANOMALOUS_STATUS),
        "by_strategy": attributions["strategy"],
        "by_signal_family": attributions["signal_family"],
        "by_source_wallet": attributions["source_wallet"],
        "by_market_category": attributions["market_category"],
        "by_asset": attributions["asset"],
        "attribution": attributions,
        "best_strategy": _best(attributions["strategy"]),
        "worst_strategy": _worst(attributions["strategy"]),
        "best_wallet": _best(attributions["source_wallet"]),
        "worst_wallet": _worst(attributions["source_wallet"]),
    }


def portfolio_trade_log(db_path: str | Path, *, limit: int = 25) -> list[dict[str, Any]]:
    migrate_paper_portfolio(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id DESC LIMIT ?", (max(int(limit), 0),)).fetchall()
        blocked = conn.execute(
            """
            SELECT o.*, r.run_timestamp FROM paper_orders o
            LEFT JOIN paper_positions p ON p.order_id=o.id
            LEFT JOIN paper_runs r ON r.id=o.run_id
            WHERE o.status='blocked' AND p.paper_position_id IS NULL ORDER BY o.id DESC LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    result = [_trade_row(row) for row in rows]
    for row in blocked:
        raw = _json(row["raw_json"])
        result.append({
            "id": f"blocked-{row['id']}", "timestamp": str(row["created_timestamp"] or row["run_timestamp"] or ""),
            "market_title": str(row["title"] or row["market_id"] or "unknown"), "market_id": str(row["market_id"] or ""),
            "outcome": str(row["side"] or ""), "side": str(row["side"] or ""), "strategy": str(row["strategy"] or "unknown"),
            "signal_family": str(raw.get("signal_family") or row["strategy"] or "unknown"), "entry_price": _number(row["simulated_price"]),
            "exit_price": None, "stake": 0.0, "notional": 0.0, "shares": 0.0, "status": "blocked",
            "realized_pnl": 0.0, "unrealized_pnl": 0.0, "source_wallet": raw.get("source_wallet") or raw.get("wallet"),
            "reason": row["blocked_reason"] or raw.get("blocked_reason") or "paper portfolio limit", "opened_at": str(row["run_timestamp"] or ""),
            "closed_at": None, "pnl": 0.0, "confidence": _number(raw.get("confidence")),
            "validation_score": _number(raw.get("validation_score")), "market_category": str(raw.get("market_category") or raw.get("category") or "unknown"),
        })
    result.sort(key=lambda item: str(item.get("timestamp") or item.get("opened_at") or ""), reverse=True)
    return result[: max(int(limit), 0)]


def _trade_row(row: Any) -> dict[str, Any]:
    status = str(row["status"] or "").lower()
    is_open = status in OPEN_STATUSES
    return {
        "id": str(row["paper_position_id"]), "timestamp": str(row["entry_timestamp"] or ""),
        "market_title": str(row["title"] or row["market_id"] or "unknown"), "market_id": str(row["market_id"] or ""),
        "outcome": str(row["side"] or ""), "side": str(row["side"] or ""), "strategy": str(row["strategy"] or "unknown"),
        "signal_family": str(row["signal_family"] or row["strategy"] or "unknown"), "entry_price": _number(row["entry_price"]),
        "exit_price": _number(row["exit_price"]) if row["exit_price"] is not None else None, "stake": _number(row["notional"]),
        "notional": _number(row["notional"]), "shares": _number(row["shares"]), "status": status,
        "realized_pnl": _number(row["realized_pnl"]), "unrealized_pnl": _number(row["unrealized_pnl"]) if is_open else 0.0,
        "source_wallet": row["source_wallet"], "reason": row["reason"], "opened_at": str(row["entry_timestamp"] or ""),
        "closed_at": str(row["exit_timestamp"] or "") or None, "pnl": _number(row["unrealized_pnl"]) if is_open else _number(row["realized_pnl"]),
        "confidence": _number(row["confidence"]), "validation_score": _number(row["validation_score"]),
        "market_category": str(row["market_category"] or row["asset"] or "unknown"), "market_liquidity": _number(row["market_liquidity"]),
        "time_to_expiry_seconds": _number(row["time_to_expiry_seconds"]),
    }


def _attribution(rows: list[Any], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row[key] or "unknown")
        item = buckets.setdefault(label, {"label": label, key: label, "trade_count": 0, "open_positions": 0, "closed_positions": 0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "win_rate": 0.0})
        item["trade_count"] += 1
        if str(row["status"] or "").lower() in OPEN_STATUSES:
            item["open_positions"] += 1
            item["unrealized_pnl"] += _number(row["unrealized_pnl"])
        else:
            item["closed_positions"] += 1
            item["realized_pnl"] += _number(row["realized_pnl"])
            if _number(row["realized_pnl"]) > 0:
                item["win_rate"] += 1
    for item in buckets.values():
        item["realized_pnl"] = round(item["realized_pnl"], 6)
        item["unrealized_pnl"] = round(item["unrealized_pnl"], 6)
        item["total_pnl"] = round(item["realized_pnl"] + item["unrealized_pnl"], 6)
        item["win_rate"] = round(_ratio(item["win_rate"], item["closed_positions"]), 6)
    return dict(sorted(buckets.items()))


def _best(items: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    return max(items.values(), key=lambda item: (float(item["total_pnl"]), item["label"]), default=None)


def _worst(items: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    return min(items.values(), key=lambda item: (float(item["total_pnl"]), item["label"]), default=None)


def _daily_returns(curve: list[dict[str, Any]], starting: float) -> list[float]:
    by_day: dict[str, float] = {}
    for row in curve:
        day = str(row.get("timestamp") or "")[:10]
        if day:
            by_day[day] = _number(row.get("equity"))
    returns: list[float] = []
    previous = starting
    for _day, value in sorted(by_day.items()):
        returns.append(_ratio(value - previous, previous))
        previous = value
    return returns


def _period_return(curve: list[dict[str, Any]], *, days: int, starting_bankroll: float) -> float:
    if not curve:
        return 0.0
    latest = _parse_time(curve[-1].get("timestamp"))
    if latest is None:
        return 0.0
    cutoff = latest.timestamp() - (days * 86400)
    prior = starting_bankroll
    for row in curve:
        timestamp = _parse_time(row.get("timestamp"))
        if timestamp and timestamp.timestamp() <= cutoff:
            prior = _number(row.get("equity"))
    return round(_ratio(_number(curve[-1].get("equity")) - prior, prior), 6)


def _average_holding_seconds(rows: list[Any]) -> float:
    durations = []
    for row in rows:
        start, end = _parse_time(row["entry_timestamp"]), _parse_time(row["exit_timestamp"])
        if start and end:
            durations.append(max(0.0, (end - start).total_seconds()))
    return mean(durations) if durations else 0.0


def _max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def _max_drawdown_pct(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return worst


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    stdev = pstdev(returns)
    return 0.0 if stdev == 0 else mean(returns) / stdev * math.sqrt(252)


def _add_columns(conn: Any, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _mark_price(row: Any) -> float:
    return min(1.0, max(0.0, _number(row["current_price"])))


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
