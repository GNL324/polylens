from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, DEFAULT_STARTING_BANKROLL
from src.sqlite_utils import closing_connection

POLYMARKET_ANALYTICS_TRADER_BASE_URL = "https://polymarketanalytics.com/traders"


def init_paper_portfolio_db(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_portfolio_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                trade_id INTEGER,
                paper_position_id INTEGER,
                strategy TEXT,
                wallet TEXT,
                market TEXT,
                side TEXT,
                action TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                entry_price REAL,
                exit_price REAL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                fees REAL NOT NULL DEFAULT 0,
                cash_balance_before REAL NOT NULL DEFAULT 0,
                cash_balance_after REAL NOT NULL DEFAULT 0,
                portfolio_value REAL NOT NULL DEFAULT 0,
                available_buying_power REAL NOT NULL DEFAULT 0,
                position_size REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(event_type, paper_position_id)
            );
            CREATE TABLE IF NOT EXISTS paper_balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                run_id INTEGER,
                cash REAL NOT NULL,
                invested_capital REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_equity REAL NOT NULL,
                drawdown REAL NOT NULL,
                exposure REAL NOT NULL,
                open_positions INTEGER NOT NULL,
                closed_positions INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_trade_attribution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_position_id INTEGER NOT NULL UNIQUE,
                trade_id INTEGER,
                strategy TEXT,
                wallet TEXT,
                signal_family TEXT,
                market TEXT,
                side TEXT,
                gross_profit REAL NOT NULL DEFAULT 0,
                gross_loss REAL NOT NULL DEFAULT 0,
                net_pnl REAL NOT NULL DEFAULT 0,
                fees REAL NOT NULL DEFAULT 0,
                duration_seconds REAL NOT NULL DEFAULT 0,
                holding_time_seconds REAL NOT NULL DEFAULT 0,
                exit_reason TEXT,
                market_resolution TEXT,
                confidence_score REAL,
                opened_at TEXT,
                closed_at TEXT,
                roi REAL NOT NULL DEFAULT 0,
                notional REAL NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_paper_ledger_timestamp ON paper_portfolio_ledger(timestamp);
            CREATE INDEX IF NOT EXISTS idx_paper_ledger_wallet ON paper_portfolio_ledger(wallet);
            CREATE INDEX IF NOT EXISTS idx_paper_ledger_strategy ON paper_portfolio_ledger(strategy);
            CREATE INDEX IF NOT EXISTS idx_paper_balance_timestamp ON paper_balance_snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_paper_attr_wallet ON paper_trade_attribution(wallet);
            CREATE INDEX IF NOT EXISTS idx_paper_attr_strategy ON paper_trade_attribution(strategy);
            """
        )


def record_position_opened(
    db_path: str | Path,
    *,
    paper_position_id: int,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> None:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        position = conn.execute("SELECT * FROM paper_positions WHERE paper_position_id=?", (paper_position_id,)).fetchone()
        if position is None:
            return
        order = conn.execute("SELECT * FROM paper_orders WHERE id=?", (position["order_id"],)).fetchone()
        metadata = _metadata_from_order(order)
        metrics_after = _portfolio_metrics(conn, starting_bankroll=starting_bankroll)
        notional = _float(position["notional"])
        cash_before = metrics_after["cash"] + notional
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_portfolio_ledger (
                timestamp, event_type, trade_id, paper_position_id, strategy, wallet, market, side, action,
                quantity, entry_price, exit_price, realized_pnl, unrealized_pnl, fees, cash_balance_before,
                cash_balance_after, portfolio_value, available_buying_power, position_size, notes, raw_json
            ) VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, 'BUY', ?, ?, NULL, 0, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position["entry_timestamp"],
                position["order_id"],
                position["paper_position_id"],
                position["strategy"],
                metadata["wallet"],
                position["title"],
                position["side"],
                _float(position["shares"]),
                _float(position["entry_price"]),
                _float(position["unrealized_pnl"]),
                round(cash_before, 6),
                metrics_after["cash"],
                metrics_after["total_equity"],
                metrics_after["cash"],
                notional,
                "simulated paper buy",
                json.dumps(metadata, sort_keys=True),
            ),
        )
        _record_balance_snapshot(conn, run_id=None, starting_bankroll=starting_bankroll, timestamp=str(position["entry_timestamp"] or _utc_now()))


def record_position_closed(
    db_path: str | Path,
    *,
    paper_position_id: int,
    exit_reason: str = "simulated_exit",
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> None:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        position = conn.execute("SELECT * FROM paper_positions WHERE paper_position_id=?", (paper_position_id,)).fetchone()
        if position is None:
            return
        order = conn.execute("SELECT * FROM paper_orders WHERE id=?", (position["order_id"],)).fetchone()
        settlement = conn.execute("SELECT * FROM paper_settlements WHERE paper_position_id=? ORDER BY id DESC LIMIT 1", (paper_position_id,)).fetchone()
        metadata = _metadata_from_order(order)
        metrics_after = _portfolio_metrics(conn, starting_bankroll=starting_bankroll)
        notional = _float(position["notional"])
        pnl = _float(position["realized_pnl"])
        cash_before = metrics_after["cash"] - notional - pnl
        timestamp = str(position["exit_timestamp"] or (settlement["exit_timestamp"] if settlement else "") or _utc_now())
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_portfolio_ledger (
                timestamp, event_type, trade_id, paper_position_id, strategy, wallet, market, side, action,
                quantity, entry_price, exit_price, realized_pnl, unrealized_pnl, fees, cash_balance_before,
                cash_balance_after, portfolio_value, available_buying_power, position_size, notes, raw_json
            ) VALUES (?, 'CLOSE', ?, ?, ?, ?, ?, ?, 'SELL', ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                position["order_id"],
                position["paper_position_id"],
                position["strategy"],
                metadata["wallet"],
                position["title"],
                position["side"],
                _float(position["shares"]),
                _float(position["entry_price"]),
                _float(position["exit_price"]),
                pnl,
                round(cash_before, 6),
                metrics_after["cash"],
                metrics_after["total_equity"],
                metrics_after["cash"],
                notional,
                exit_reason,
                json.dumps(metadata, sort_keys=True),
            ),
        )
        _upsert_trade_attribution(conn, position=position, order=order, settlement=settlement, metadata=metadata, exit_reason=exit_reason)
        _record_balance_snapshot(conn, run_id=settlement["run_id"] if settlement else None, starting_bankroll=starting_bankroll, timestamp=timestamp)


def record_balance_snapshot(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    run_id: int | None = None,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
    timestamp: str | None = None,
) -> dict[str, Any]:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        return _record_balance_snapshot(conn, run_id=run_id, starting_bankroll=starting_bankroll, timestamp=timestamp or _utc_now())


def rebuild_portfolio_analytics(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> dict[str, Any]:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        if not _has_table(conn, "paper_positions"):
            snapshot = _record_balance_snapshot(conn, run_id=None, starting_bankroll=starting_bankroll, timestamp=_utc_now())
            return {"positions_processed": 0, "snapshot": snapshot}
        positions = conn.execute("SELECT paper_position_id, status FROM paper_positions ORDER BY paper_position_id").fetchall()
    for position in positions:
        record_position_opened(db_path, paper_position_id=int(position["paper_position_id"]), starting_bankroll=starting_bankroll)
        if str(position["status"] or "").lower() == "closed":
            record_position_closed(db_path, paper_position_id=int(position["paper_position_id"]), starting_bankroll=starting_bankroll)
    snapshot = record_balance_snapshot(db_path, starting_bankroll=starting_bankroll)
    return {"positions_processed": len(positions), "snapshot": snapshot}


def portfolio_report(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
    now: datetime | None = None,
) -> dict[str, Any]:
    init_paper_portfolio_db(db_path)
    now = _as_utc(now or datetime.now(timezone.utc))
    with closing_connection(db_path) as conn:
        _ensure_attribution_current(conn)
        metrics = _portfolio_metrics(conn, starting_bankroll=starting_bankroll)
        ledger = [_row_dict(row) for row in conn.execute("SELECT * FROM paper_portfolio_ledger ORDER BY timestamp DESC, id DESC LIMIT 25").fetchall()]
        balance = [_row_dict(row) for row in conn.execute("SELECT * FROM paper_balance_snapshots ORDER BY timestamp, id").fetchall()]
        trades = [_row_dict(row) for row in conn.execute("SELECT * FROM paper_trade_attribution ORDER BY closed_at DESC, id DESC").fetchall()]
        open_positions = (
            [_row_dict(row) for row in conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY entry_timestamp DESC").fetchall()]
            if _has_table(conn, "paper_positions")
            else []
        )
    closed = trades
    today = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    pnl_today = _pnl_since(closed, today)
    pnl_7d = _pnl_since(closed, now - timedelta(days=7))
    pnl_30d = _pnl_since(closed, now - timedelta(days=30))
    largest_winner = max(closed, key=lambda row: _float(row.get("net_pnl")), default=None)
    largest_loser = min(closed, key=lambda row: _float(row.get("net_pnl")), default=None)
    wallet_stats = wallet_attribution(db_path, starting_bankroll=starting_bankroll)
    strategy_stats = strategy_attribution(db_path, starting_bankroll=starting_bankroll, now=now)
    return {
        "portfolio": metrics,
        "ledger": ledger,
        "balance_history": balance,
        "equity_curve": [{"timestamp": row["timestamp"], "equity": row["total_equity"], "drawdown": row["drawdown"]} for row in balance],
        "recent_trades": closed[:10],
        "open_positions": open_positions[:10],
        "pnl": {
            "today": round(pnl_today, 6),
            "seven_day": round(pnl_7d, 6),
            "thirty_day": round(pnl_30d, 6),
            "all_time": metrics["realized_pnl"],
        },
        "largest_winner": largest_winner,
        "largest_loser": largest_loser,
        "wallet_attribution": wallet_stats,
        "strategy_attribution": strategy_stats,
        "most_profitable_wallet": wallet_stats[0] if wallet_stats else None,
        "worst_wallet": wallet_stats[-1] if wallet_stats else None,
        "best_strategy": strategy_stats[0] if strategy_stats else None,
        "worst_strategy": strategy_stats[-1] if strategy_stats else None,
    }


def trade_detail(db_path: str | Path, trade_id: int) -> dict[str, Any] | None:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM paper_trade_attribution WHERE paper_position_id=? OR trade_id=? ORDER BY id DESC LIMIT 1", (trade_id, trade_id)).fetchone()
    return _row_dict(row) if row else None


def wallet_attribution(db_path: str | Path = DEFAULT_PAPER_TRADING_DB, *, starting_bankroll: float = DEFAULT_STARTING_BANKROLL) -> list[dict[str, Any]]:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        _ensure_attribution_current(conn)
        rows = [_row_dict(row) for row in conn.execute("SELECT * FROM paper_trade_attribution").fetchall()]
        open_rows = (
            [_row_dict(row) for row in conn.execute("SELECT p.*, o.raw_json FROM paper_positions p LEFT JOIN paper_orders o ON o.id=p.order_id WHERE p.status='open'").fetchall()]
            if _has_table(conn, "paper_positions") and _has_table(conn, "paper_orders")
            else []
        )
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        wallet = _valid_wallet(row.get("wallet")) or "unknown"
        bucket = _wallet_bucket(buckets, wallet)
        _add_closed_trade(bucket, row)
    for row in open_rows:
        metadata = _loads(row.get("raw_json"))
        wallet = _valid_wallet(_first(metadata, "wallet", "wallet_address", "trader_wallet", "address", "maker", "proxy_wallet")) or "unknown"
        bucket = _wallet_bucket(buckets, wallet)
        bucket["unrealized_pnl"] += _float(row.get("unrealized_pnl"))
        bucket["open_positions"] += 1
    return _finalize_buckets(buckets, starting_bankroll=starting_bankroll)


def strategy_attribution(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    init_paper_portfolio_db(db_path)
    now = _as_utc(now or datetime.now(timezone.utc))
    with closing_connection(db_path) as conn:
        _ensure_attribution_current(conn)
        rows = [_row_dict(row) for row in conn.execute("SELECT * FROM paper_trade_attribution").fetchall()]
        open_rows = (
            [_row_dict(row) for row in conn.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall()]
            if _has_table(conn, "paper_positions")
            else []
        )
    total_abs = sum(abs(_float(row["net_pnl"])) for row in rows) or 1.0
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        bucket = _strategy_bucket(buckets, strategy)
        _add_closed_trade(bucket, row)
        closed_at = _parse_time(row.get("closed_at"))
        if closed_at and closed_at >= datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc):
            bucket["daily_pnl"] += _float(row.get("net_pnl"))
        if closed_at and closed_at >= now - timedelta(days=7):
            bucket["weekly_pnl"] += _float(row.get("net_pnl"))
    for row in open_rows:
        bucket = _strategy_bucket(buckets, str(row.get("strategy") or "unknown"))
        bucket["capital_allocation"] += _float(row.get("notional"))
        bucket["unrealized_pnl"] += _float(row.get("unrealized_pnl"))
    result = _finalize_buckets(buckets, starting_bankroll=starting_bankroll)
    for row in result:
        row["daily_pnl"] = round(row.get("daily_pnl", 0.0), 6)
        row["weekly_pnl"] = round(row.get("weekly_pnl", 0.0), 6)
        row["contribution_pct"] = round(abs(row["realized_pnl"]) / total_abs, 6)
    return result


def reconstruct_portfolio_value_at(db_path: str | Path, timestamp: str) -> dict[str, Any]:
    init_paper_portfolio_db(db_path)
    with closing_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM paper_balance_snapshots WHERE timestamp <= ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (timestamp,),
        ).fetchone()
    return _row_dict(row) if row else {}


def replay_portfolio(db_path: str | Path, *, starting_bankroll: float = DEFAULT_STARTING_BANKROLL) -> list[dict[str, Any]]:
    init_paper_portfolio_db(db_path)
    cash = float(starting_bankroll)
    positions: dict[int, float] = {}
    history: list[dict[str, Any]] = []
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_portfolio_ledger ORDER BY timestamp, id").fetchall()
    for row in rows:
        position_id = int(row["paper_position_id"] or 0)
        if row["event_type"] == "OPEN":
            cash -= _float(row["position_size"])
            positions[position_id] = _float(row["position_size"])
        elif row["event_type"] == "CLOSE":
            cash += positions.pop(position_id, _float(row["position_size"])) + _float(row["realized_pnl"])
        history.append({"timestamp": row["timestamp"], "cash": round(cash, 6), "open_positions": len(positions), "portfolio_value": _float(row["portfolio_value"])})
    return history


def _record_balance_snapshot(conn: sqlite3.Connection, *, run_id: int | None, starting_bankroll: float, timestamp: str) -> dict[str, Any]:
    metrics = _portfolio_metrics(conn, starting_bankroll=starting_bankroll)
    conn.execute(
        """
        INSERT INTO paper_balance_snapshots (
            timestamp, run_id, cash, invested_capital, unrealized_pnl, realized_pnl,
            total_equity, drawdown, exposure, open_positions, closed_positions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            run_id,
            metrics["cash"],
            metrics["invested_capital"],
            metrics["unrealized_pnl"],
            metrics["realized_pnl"],
            metrics["total_equity"],
            metrics["drawdown"],
            metrics["exposure"],
            metrics["open_positions"],
            metrics["closed_positions"],
        ),
    )
    return metrics


def _portfolio_metrics(conn: sqlite3.Connection, *, starting_bankroll: float) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM paper_positions").fetchall() if _has_table(conn, "paper_positions") else []
    open_rows = [row for row in rows if str(row["status"]).lower() == "open"]
    closed = [row for row in rows if str(row["status"]).lower() == "closed"]
    invested = sum(_float(row["notional"]) for row in open_rows)
    unrealized = sum(_float(row["unrealized_pnl"]) for row in open_rows)
    realized = sum(_float(row["realized_pnl"]) for row in closed)
    total_equity = float(starting_bankroll) + realized + unrealized
    cash = total_equity - invested
    prior_equity = [float(row["total_equity"]) for row in conn.execute("SELECT total_equity FROM paper_balance_snapshots").fetchall()]
    peak = max([float(starting_bankroll), total_equity, *prior_equity])
    drawdown = total_equity - peak
    return {
        "cash": round(cash, 6),
        "invested_capital": round(invested, 6),
        "unrealized_pnl": round(unrealized, 6),
        "realized_pnl": round(realized, 6),
        "total_equity": round(total_equity, 6),
        "drawdown": round(drawdown, 6),
        "exposure": round(_ratio(invested, total_equity), 6),
        "open_positions": len(open_rows),
        "closed_positions": len(closed),
        "available_buying_power": round(max(0.0, cash), 6),
        "portfolio_value": round(total_equity, 6),
    }


def _upsert_trade_attribution(conn: sqlite3.Connection, *, position: Any, order: Any, settlement: Any, metadata: dict[str, Any], exit_reason: str) -> None:
    pnl = _float(position["realized_pnl"])
    opened = _parse_time(position["entry_timestamp"])
    closed = _parse_time(position["exit_timestamp"])
    duration = (closed - opened).total_seconds() if opened and closed else 0.0
    conn.execute(
        """
        INSERT INTO paper_trade_attribution (
            paper_position_id, trade_id, strategy, wallet, signal_family, market, side,
            gross_profit, gross_loss, net_pnl, fees, duration_seconds, holding_time_seconds,
            exit_reason, market_resolution, confidence_score, opened_at, closed_at, roi, notional, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_position_id) DO UPDATE SET
            net_pnl=excluded.net_pnl,
            gross_profit=excluded.gross_profit,
            gross_loss=excluded.gross_loss,
            closed_at=excluded.closed_at,
            exit_reason=excluded.exit_reason,
            market_resolution=excluded.market_resolution
        """,
        (
            position["paper_position_id"],
            position["order_id"],
            position["strategy"],
            metadata["wallet"],
            metadata["signal_family"],
            position["title"],
            position["side"],
            max(0.0, pnl),
            min(0.0, pnl),
            pnl,
            duration,
            duration,
            exit_reason or (settlement["reason"] if settlement else ""),
            _market_resolution(position, settlement),
            metadata["confidence_score"],
            position["entry_timestamp"],
            position["exit_timestamp"],
            _float(position["roi"]),
            _float(position["notional"]),
            json.dumps(metadata, sort_keys=True),
        ),
    )


def _ensure_attribution_current(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "paper_positions"):
        return
    missing = conn.execute(
        """
        SELECT p.*
        FROM paper_positions p
        LEFT JOIN paper_trade_attribution a ON a.paper_position_id = p.paper_position_id
        WHERE p.status='closed' AND a.paper_position_id IS NULL
        """
    ).fetchall()
    for position in missing:
        order = conn.execute("SELECT * FROM paper_orders WHERE id=?", (position["order_id"],)).fetchone() if _has_table(conn, "paper_orders") else None
        settlement = (
            conn.execute("SELECT * FROM paper_settlements WHERE paper_position_id=? ORDER BY id DESC LIMIT 1", (position["paper_position_id"],)).fetchone()
            if _has_table(conn, "paper_settlements")
            else None
        )
        _upsert_trade_attribution(conn, position=position, order=order, settlement=settlement, metadata=_metadata_from_order(order), exit_reason=settlement["reason"] if settlement else "historical")


def _metadata_from_order(order: Any) -> dict[str, Any]:
    raw = _loads(order["raw_json"] if order else "{}")
    return {
        "wallet": _valid_wallet(_first(raw, "wallet", "wallet_address", "trader_wallet", "address", "maker", "proxy_wallet")),
        "signal_family": str(_first(raw, "signal_family", "signal_type", "strategy", "strategy_profile") or (order["strategy"] if order else "") or "unknown"),
        "confidence_score": _nullable_float(_first(raw, "confidence_score", "confidence", "score", "ranking_score")),
        "raw": raw,
    }


def _wallet_bucket(buckets: dict[str, dict[str, Any]], wallet: str) -> dict[str, Any]:
    return buckets.setdefault(
        wallet,
        {
            "wallet": wallet,
            "polymarket_analytics_url": polymarket_analytics_url(wallet),
            "trades_generated": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "roi": 0.0,
            "win_rate": 0.0,
            "average_return": 0.0,
            "expectancy": 0.0,
            "sharpe_score": 0.0,
            "drawdown": 0.0,
            "average_holding_period_seconds": 0.0,
            "open_positions": 0,
            "_returns": [],
            "_durations": [],
            "_wins": 0,
        },
    )


def _strategy_bucket(buckets: dict[str, dict[str, Any]], strategy: str) -> dict[str, Any]:
    bucket = _wallet_bucket(buckets, strategy)
    bucket["strategy"] = strategy
    bucket.pop("wallet", None)
    bucket.pop("polymarket_analytics_url", None)
    bucket.setdefault("daily_pnl", 0.0)
    bucket.setdefault("weekly_pnl", 0.0)
    bucket.setdefault("capital_allocation", 0.0)
    bucket.setdefault("contribution_pct", 0.0)
    return bucket


def _add_closed_trade(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    pnl = _float(row.get("net_pnl"))
    bucket["trades_generated"] += 1
    bucket["realized_pnl"] += pnl
    bucket["average_holding_period_seconds"] += _float(row.get("holding_time_seconds"))
    bucket["_durations"].append(_float(row.get("holding_time_seconds")))
    bucket["_returns"].append(pnl)
    if pnl > 0:
        bucket["_wins"] += 1


def _finalize_buckets(buckets: dict[str, dict[str, Any]], *, starting_bankroll: float) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for bucket in buckets.values():
        returns = bucket.pop("_returns", [])
        bucket.pop("_durations", None)
        wins = bucket.pop("_wins", 0)
        trades = int(bucket["trades_generated"] or 0)
        bucket["realized_pnl"] = round(float(bucket["realized_pnl"]), 6)
        bucket["unrealized_pnl"] = round(float(bucket["unrealized_pnl"]), 6)
        bucket["roi"] = round(_ratio(bucket["realized_pnl"] + bucket["unrealized_pnl"], starting_bankroll), 6)
        bucket["win_rate"] = round(_ratio(wins, trades), 6)
        bucket["average_return"] = round(mean(returns), 6) if returns else 0.0
        bucket["expectancy"] = bucket["average_return"]
        bucket["sharpe_score"] = round(_sharpe(returns), 6)
        bucket["drawdown"] = round(_max_drawdown(returns), 6)
        bucket["average_holding_period_seconds"] = round(_ratio(bucket["average_holding_period_seconds"], trades), 6)
        bucket["trade_count"] = trades
        bucket["total_pnl"] = round(bucket["realized_pnl"] + bucket["unrealized_pnl"], 6)
        bucket["average_duration_seconds"] = bucket["average_holding_period_seconds"]
        if "capital_allocation" in bucket:
            bucket["capital_allocation"] = round(float(bucket["capital_allocation"]), 6)
        result.append(bucket)
    return sorted(result, key=lambda item: (item["realized_pnl"] + item["unrealized_pnl"], item.get("win_rate", 0.0)), reverse=True)


def polymarket_analytics_url(wallet: Any) -> str | None:
    valid = _valid_wallet(wallet)
    return f"{POLYMARKET_ANALYTICS_TRADER_BASE_URL}/{valid}" if valid else None


def _valid_wallet(wallet: Any) -> str | None:
    text = str(wallet or "").strip()
    if len(text) == 42 and text.startswith("0x") and all(ch in "0123456789abcdefABCDEF" for ch in text[2:]):
        return text
    return None


def _market_resolution(position: Any, settlement: Any) -> str:
    if settlement and settlement["reason"]:
        return str(settlement["reason"])
    return "won" if _float(position["realized_pnl"]) > 0 else "lost" if _float(position["realized_pnl"]) < 0 else "flat"


def _pnl_since(rows: list[dict[str, Any]], cutoff: datetime) -> float:
    total = 0.0
    for row in rows:
        closed = _parse_time(row.get("closed_at"))
        if closed and closed >= cutoff:
            total += _float(row.get("net_pnl"))
    return total


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _nullable_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else float(numerator) / float(denominator)


def _sharpe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    stdev = pstdev(values)
    return 0.0 if stdev == 0 else mean(values) / stdev * math.sqrt(len(values))


def _max_drawdown(values: list[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return drawdown


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
