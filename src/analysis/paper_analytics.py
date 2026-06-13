from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, DEFAULT_STARTING_BANKROLL, init_paper_trading_db
from src.sqlite_utils import closing_connection


def build_paper_analytics_report(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    starting_equity: float = DEFAULT_STARTING_BANKROLL,
    now: datetime | None = None,
) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    now = now or datetime.now(timezone.utc)
    with closing_connection(db_path) as conn:
        positions = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id").fetchall()
        settlements = conn.execute("SELECT * FROM paper_settlements ORDER BY exit_timestamp, id").fetchall()
        equity_rows = conn.execute("SELECT * FROM paper_equity_curve ORDER BY timestamp, id").fetchall()

    closed = [row for row in positions if row["status"] == "closed"]
    open_rows = [row for row in positions if row["status"] == "open"]
    realized = sum(_float(row["realized_pnl"]) for row in closed)
    unrealized = sum(_float(row["unrealized_pnl"]) for row in open_rows)
    current = starting_equity + realized + unrealized
    pnl_values = [_float(row["realized_pnl"]) for row in closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    drawdowns = _drawdowns(equity_rows, starting_equity)
    strategy = _breakdown(positions, "strategy")
    asset = _breakdown(positions, "asset")
    return {
        "equity": {
            "current": round(current, 6),
            "starting": round(float(starting_equity), 6),
            "net_pnl": round(realized + unrealized, 6),
            "realized_pnl": round(realized, 6),
            "unrealized_pnl": round(unrealized, 6),
            "roi": round(_ratio(realized + unrealized, starting_equity), 6),
        },
        "trading": {
            "total_trades": len(positions),
            "open_trades": len(open_rows),
            "closed_trades": len(closed),
            "win_rate": round(_ratio(len(wins), len(closed)), 6),
            "loss_rate": round(_ratio(len(losses), len(closed)), 6),
            "expectancy": round(mean(pnl_values), 6) if pnl_values else 0.0,
            "profit_factor": round(_profit_factor(wins, losses), 6),
        },
        "risk": {
            "max_drawdown": round(abs(min(drawdowns) if drawdowns else 0.0), 6),
            "average_drawdown": round(abs(mean([value for value in drawdowns if value < 0])), 6) if any(value < 0 for value in drawdowns) else 0.0,
            "longest_drawdown": _longest_drawdown(equity_rows, starting_equity),
            "recovery_factor": round(_ratio(realized + unrealized, abs(min(drawdowns) if drawdowns else 0.0)), 6),
        },
        "strategy_breakdown": strategy,
        "asset_breakdown": asset,
        "time_analysis": {
            "pnl_by_hour": _pnl_by_period(settlements, "%Y-%m-%dT%H:00:00Z"),
            "pnl_by_day": _pnl_by_period(settlements, "%Y-%m-%d"),
            "pnl_last_24h": round(_pnl_since(settlements, now - timedelta(hours=24)), 6),
            "pnl_last_7d": round(_pnl_since(settlements, now - timedelta(days=7)), 6),
        },
        "best_strategy": _best(strategy),
    }


def _breakdown(rows: list[Any], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row[key] or "unknown")
        bucket = buckets.setdefault(
            label,
            {"trades": 0, "open_trades": 0, "closed_trades": 0, "pnl": 0.0, "unrealized_pnl": 0.0, "notional": 0.0, "win_rate": 0.0, "roi": 0.0, "exposure": 0.0},
        )
        bucket["trades"] += 1
        bucket["notional"] += _float(row["notional"])
        if row["status"] == "closed":
            bucket["closed_trades"] += 1
            bucket["pnl"] += _float(row["realized_pnl"])
        else:
            bucket["open_trades"] += 1
            bucket["unrealized_pnl"] += _float(row["unrealized_pnl"])
            bucket["exposure"] += _float(row["notional"])
    for label, bucket in buckets.items():
        wins = sum(1 for row in rows if str(row[key] or "unknown") == label and row["status"] == "closed" and _float(row["realized_pnl"]) > 0)
        bucket["pnl"] = round(bucket["pnl"], 6)
        bucket["unrealized_pnl"] = round(bucket["unrealized_pnl"], 6)
        bucket["notional"] = round(bucket["notional"], 6)
        bucket["exposure"] = round(bucket["exposure"], 6)
        bucket["win_rate"] = round(_ratio(wins, bucket["closed_trades"]), 6)
        bucket["roi"] = round(_ratio(bucket["pnl"] + bucket["unrealized_pnl"], bucket["notional"]), 6)
    return dict(sorted(buckets.items()))


def _best(strategy: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not strategy:
        return None
    name, row = max(strategy.items(), key=lambda item: (item[1].get("roi", 0.0), item[1].get("pnl", 0.0), item[0]))
    return {"name": name, "roi": row["roi"], "pnl": row["pnl"], "trades": row["trades"]}


def _drawdowns(rows: list[Any], starting_equity: float) -> list[float]:
    values = [float(starting_equity)] + [_float(row["equity"]) for row in rows]
    peak = values[0] if values else 0.0
    drawdowns: list[float] = []
    for value in values:
        peak = max(peak, value)
        drawdowns.append(value - peak)
    return drawdowns


def _longest_drawdown(rows: list[Any], starting_equity: float) -> int:
    peak = float(starting_equity)
    current = 0
    longest = 0
    for row in rows:
        value = _float(row["equity"])
        if value >= peak:
            peak = value
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _pnl_by_period(rows: list[Any], fmt: str) -> dict[str, float]:
    buckets: dict[str, float] = defaultdict(float)
    for row in rows:
        timestamp = _parse_time(row["exit_timestamp"])
        if timestamp is None:
            continue
        buckets[timestamp.strftime(fmt)] += _float(row["pnl"])
    return {key: round(value, 6) for key, value in sorted(buckets.items())}


def _pnl_since(rows: list[Any], cutoff: datetime) -> float:
    total = 0.0
    for row in rows:
        timestamp = _parse_time(row["exit_timestamp"])
        if timestamp is not None and timestamp >= cutoff:
            total += _float(row["pnl"])
    return total


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _profit_factor(wins: list[float], losses: list[float]) -> float:
    if not losses:
        return sum(wins) if wins else 0.0
    return _ratio(sum(wins), abs(sum(losses)))


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
