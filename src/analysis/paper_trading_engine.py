from __future__ import annotations

import json
import math
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Callable

from src.analysis.opportunity_feed import get_paper_trading_opportunities
from src.sqlite_utils import closing_connection

DEFAULT_PAPER_TRADING_DB = "data/paper_trading.db"
DEFAULT_STARTING_BANKROLL = 100.0
DEFAULT_RISK_PER_TRADE = 0.02
DEFAULT_MAX_OPEN_POSITIONS = 10
DEFAULT_MAX_STRATEGY_EXPOSURE = 0.20


@dataclass
class PaperTradingConfig:
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS
    max_strategy_exposure: float = DEFAULT_MAX_STRATEGY_EXPOSURE
    opportunity_limit: int = 25


@dataclass
class PaperOpportunity:
    opportunity_id: str
    strategy: str
    market_id: str
    title: str
    asset: str
    side: str
    entry_price: float
    target_price: float | None
    estimated_roi: float
    ranking_score: float
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def init_paper_trading_db(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                opportunities_seen INTEGER NOT NULL,
                orders_created INTEGER NOT NULL,
                positions_opened INTEGER NOT NULL,
                positions_settled INTEGER NOT NULL,
                equity REAL NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                market_id TEXT NOT NULL,
                title TEXT NOT NULL,
                asset TEXT NOT NULL,
                simulated_price REAL NOT NULL,
                stake REAL NOT NULL,
                status TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES paper_runs(id)
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                paper_position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL UNIQUE,
                strategy TEXT NOT NULL,
                market_id TEXT NOT NULL,
                title TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                entry_price REAL NOT NULL,
                shares REAL NOT NULL,
                notional REAL NOT NULL,
                status TEXT NOT NULL,
                current_price REAL NOT NULL,
                exit_timestamp TEXT,
                exit_price REAL,
                realized_pnl REAL,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                roi REAL
            );
            CREATE TABLE IF NOT EXISTS paper_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                paper_position_id INTEGER NOT NULL,
                exit_timestamp TEXT NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                roi REAL NOT NULL,
                reason TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES paper_runs(id),
                FOREIGN KEY(paper_position_id) REFERENCES paper_positions(paper_position_id)
            );
            CREATE TABLE IF NOT EXISTS paper_equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                equity REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                open_positions INTEGER NOT NULL,
                drawdown REAL NOT NULL,
                FOREIGN KEY(run_id) REFERENCES paper_runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status);
            CREATE INDEX IF NOT EXISTS idx_paper_positions_strategy ON paper_positions(strategy, status);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_run ON paper_orders(run_id);
            CREATE INDEX IF NOT EXISTS idx_paper_settlements_position ON paper_settlements(paper_position_id);
            CREATE INDEX IF NOT EXISTS idx_paper_equity_run ON paper_equity_curve(run_id);
            """
        )
    from src.analysis.paper_portfolio import init_paper_portfolio_db

    init_paper_portfolio_db(db_path)


def collect_opportunities(limit: int = 25) -> list[dict[str, Any]]:
    return get_paper_trading_opportunities(limit=limit)


def run_paper_trading_engine(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    config: PaperTradingConfig | None = None,
    collectors: list[Callable[[], list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    config = config or PaperTradingConfig()
    raw = _collect_all(collectors, config.opportunity_limit)
    opportunities = sorted((_normalize_opportunity(item) for item in raw), key=lambda item: (-item.ranking_score, item.opportunity_id))
    equity_before = _current_equity(db_path, config.starting_bankroll)
    run_id = _create_run(db_path, opportunities_seen=len(opportunities), equity=equity_before)
    settled = settle_open_positions(db_path=db_path, run_id=run_id)
    orders_created = 0
    positions_opened = 0
    for opportunity in opportunities:
        if _has_position(db_path, opportunity.opportunity_id):
            continue
        if _open_position_count(db_path) >= config.max_open_positions:
            continue
        stake = calculate_position_size(db_path=db_path, strategy=opportunity.strategy, config=config)
        if stake <= 0:
            continue
        order_id = _save_order(db_path, run_id, opportunity, stake)
        orders_created += 1
        if _open_position(db_path, order_id, opportunity, stake, starting_bankroll=config.starting_bankroll):
            positions_opened += 1
    equity = record_equity_snapshot(db_path, run_id=run_id, starting_bankroll=config.starting_bankroll)
    _update_run(
        db_path,
        run_id,
        opportunities_seen=len(opportunities),
        orders_created=orders_created,
        positions_opened=positions_opened,
        positions_settled=settled,
        equity=equity["equity"],
        raw={"opportunity_ids": [item.opportunity_id for item in opportunities]},
    )
    return {
        "run_id": run_id,
        "opportunities_seen": len(opportunities),
        "orders_created": orders_created,
        "positions_opened": positions_opened,
        "positions_settled": settled,
        "open_positions": _open_position_count(db_path),
        "equity": equity["equity"],
        "realized_pnl": equity["realized_pnl"],
        "unrealized_pnl": equity["unrealized_pnl"],
    }


def calculate_position_size(
    *,
    db_path: str | Path,
    strategy: str,
    config: PaperTradingConfig,
) -> float:
    equity = _current_equity(db_path, config.starting_bankroll)
    base = equity * config.risk_per_trade
    max_strategy_exposure = equity * config.max_strategy_exposure
    current_strategy_exposure = _strategy_open_notional(db_path, strategy)
    remaining_strategy_capacity = max(0.0, max_strategy_exposure - current_strategy_exposure)
    return round(max(0.0, min(base, remaining_strategy_capacity)), 6)


def settle_open_positions(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    run_id: int | None = None,
    prices: dict[str, float] | None = None,
) -> int:
    init_paper_trading_db(db_path)
    prices = prices or {}
    settled = 0
    closed_position_ids: list[int] = []
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY paper_position_id").fetchall()
        for row in rows:
            exit_price = prices.get(str(row["opportunity_id"]))
            if exit_price is None:
                raw = json.loads(conn.execute("SELECT raw_json FROM paper_orders WHERE id=?", (row["order_id"],)).fetchone()["raw_json"])
                exit_price = _safe_float(raw.get("settlement_price") or raw.get("exit_price") or raw.get("target_price"))
            if exit_price <= 0:
                mark_price = _safe_float(prices.get(str(row["opportunity_id"])) or row["current_price"])
                unrealized = (mark_price - float(row["entry_price"])) * float(row["shares"])
                conn.execute(
                    "UPDATE paper_positions SET current_price=?, unrealized_pnl=? WHERE paper_position_id=?",
                    (mark_price, round(unrealized, 6), row["paper_position_id"]),
                )
                continue
            pnl = (exit_price - float(row["entry_price"])) * float(row["shares"])
            roi = _ratio(pnl, float(row["notional"]))
            timestamp = _utc_now()
            conn.execute(
                """
                UPDATE paper_positions
                SET status='closed', exit_timestamp=?, exit_price=?, realized_pnl=?, unrealized_pnl=0, roi=?
                WHERE paper_position_id=?
                """,
                (timestamp, exit_price, round(pnl, 6), round(roi, 6), row["paper_position_id"]),
            )
            conn.execute(
                """
                INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id or 0, row["paper_position_id"], timestamp, exit_price, round(pnl, 6), round(roi, 6), "simulated_exit"),
            )
            closed_position_ids.append(int(row["paper_position_id"]))
            settled += 1
    if closed_position_ids:
        from src.analysis.paper_portfolio import record_position_closed

        for paper_position_id in closed_position_ids:
            record_position_closed(db_path, paper_position_id=paper_position_id, exit_reason="simulated_exit")
    return settled


def performance_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB, starting_bankroll: float = DEFAULT_STARTING_BANKROLL) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id").fetchall()
    closed = [row for row in rows if row["status"] == "closed"]
    open_rows = [row for row in rows if row["status"] == "open"]
    pnl_values = [float(row["realized_pnl"] or 0.0) for row in closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    realized = round(sum(pnl_values), 6)
    unrealized = round(sum(float(row["unrealized_pnl"] or 0.0) for row in open_rows), 6)
    return {
        "starting_bankroll": starting_bankroll,
        "equity": round(starting_bankroll + realized + unrealized, 6),
        "open_positions": len(open_rows),
        "closed_positions": len(closed),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "win_rate": round(_ratio(len(wins), len(closed)), 6),
        "expectancy": round(mean(pnl_values), 6) if pnl_values else 0.0,
        "sharpe_ratio": round(_sharpe(pnl_values), 6),
        "max_drawdown": round(_max_drawdown(_equity_values(db_path, starting_bankroll)), 6),
        "profit_factor": round(_ratio(sum(wins), abs(sum(losses))), 6) if losses else (round(sum(wins), 6) if wins else 0.0),
        "roi": round(_ratio(realized + unrealized, starting_bankroll), 6),
        "by_strategy": _segment(rows, "strategy"),
        "by_asset": _segment(rows, "asset"),
    }


def open_positions_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY paper_position_id").fetchall()
    return {"open_positions": [_row_dict(row) for row in rows], "count": len(rows)}


def equity_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_equity_curve ORDER BY id").fetchall()
    return {
        "equity_curve": [_row_dict(row) for row in rows],
        "latest_equity": float(rows[-1]["equity"]) if rows else DEFAULT_STARTING_BANKROLL,
        "max_drawdown": round(_max_drawdown([float(row["equity"]) for row in rows]), 6),
    }


def record_equity_snapshot(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    run_id: int,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
) -> dict[str, float]:
    report = performance_report(db_path, starting_bankroll=starting_bankroll)
    prior = _equity_values(db_path, starting_bankroll)
    peak = max([starting_bankroll, *prior, report["equity"]])
    drawdown = report["equity"] - peak
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_equity_curve (run_id, timestamp, equity, realized_pnl, unrealized_pnl, open_positions, drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, _utc_now(), report["equity"], report["realized_pnl"], report["unrealized_pnl"], report["open_positions"], round(drawdown, 6)),
        )
    from src.analysis.paper_portfolio import record_balance_snapshot

    record_balance_snapshot(db_path, run_id=run_id, starting_bankroll=starting_bankroll)
    return {
        "equity": report["equity"],
        "realized_pnl": report["realized_pnl"],
        "unrealized_pnl": report["unrealized_pnl"],
        "drawdown": round(drawdown, 6),
    }


def _collect_all(collectors: list[Callable[[], list[dict[str, Any]]]] | None, limit: int) -> list[dict[str, Any]]:
    if collectors is None:
        collectors = [lambda: collect_opportunities(limit=limit)]
    rows: list[dict[str, Any]] = []
    for collector in collectors:
        try:
            rows.extend(item for item in collector() if isinstance(item, dict))
        except Exception:
            continue
    return rows


def _normalize_opportunity(row: dict[str, Any]) -> PaperOpportunity:
    opportunity_id = str(row.get("id") or row.get("opportunity_id") or row.get("opportunity_key") or _stable_json(row))
    strategy = str(row.get("strategy") or row.get("strategy_profile") or row.get("opportunity_type") or row.get("market_type") or "unknown")
    entry_price = _bounded_price(row.get("entry_price") or row.get("price") or row.get("yes_ask") or row.get("best_price") or 0.5)
    estimated_roi = _safe_float(row.get("estimated_roi") or row.get("guaranteed_roi") or row.get("roi"))
    target = _safe_float(row.get("target_price") or row.get("settlement_price") or row.get("exit_price"))
    if target <= 0 and estimated_roi:
        target = entry_price * (1.0 + estimated_roi)
    return PaperOpportunity(
        opportunity_id=opportunity_id,
        strategy=strategy,
        market_id=str(row.get("market_id") or row.get("ticker") or row.get("id") or opportunity_id),
        title=str(row.get("title") or row.get("market_title") or row.get("polymarket_title") or row.get("player") or "unknown"),
        asset=str(row.get("asset") or row.get("sport") or row.get("venue") or "OTHER").upper(),
        side=str(row.get("side") or row.get("direction") or row.get("outcome") or "yes").lower(),
        entry_price=entry_price,
        target_price=target if target > 0 else None,
        estimated_roi=estimated_roi,
        ranking_score=_safe_float(row.get("ranking_score") or row.get("score")),
        raw=dict(row),
    )


def _save_order(db_path: str | Path, run_id: int, opportunity: PaperOpportunity, stake: float) -> int:
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_orders
            (run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?)
            """,
            (
                run_id,
                opportunity.opportunity_id,
                opportunity.strategy,
                opportunity.side,
                opportunity.market_id,
                opportunity.title,
                opportunity.asset,
                opportunity.entry_price,
                stake,
                json.dumps(opportunity.raw, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)


def _open_position(db_path: str | Path, order_id: int, opportunity: PaperOpportunity, stake: float, *, starting_bankroll: float = DEFAULT_STARTING_BANKROLL) -> bool:
    shares = stake / opportunity.entry_price if opportunity.entry_price > 0 else 0.0
    if shares <= 0:
        return False
    position_id: int | None = None
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_positions
            (order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                order_id,
                opportunity.opportunity_id,
                opportunity.strategy,
                opportunity.market_id,
                opportunity.title,
                opportunity.asset,
                opportunity.side,
                _utc_now(),
                opportunity.entry_price,
                round(shares, 8),
                round(stake, 6),
                opportunity.entry_price,
            ),
        )
        opened = cur.rowcount > 0
        if opened:
            row = conn.execute("SELECT paper_position_id FROM paper_positions WHERE opportunity_id=?", (opportunity.opportunity_id,)).fetchone()
            position_id = int(row["paper_position_id"]) if row else None
    if opened and position_id is not None:
        from src.analysis.paper_portfolio import record_position_opened

        record_position_opened(db_path, paper_position_id=position_id, starting_bankroll=starting_bankroll)
    return opened


def _create_run(db_path: str | Path, *, opportunities_seen: int, equity: float) -> int:
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_runs
            (run_timestamp, opportunities_seen, orders_created, positions_opened, positions_settled, equity, raw_json)
            VALUES (?, ?, 0, 0, 0, ?, '{}')
            """,
            (_utc_now(), opportunities_seen, equity),
        )
        return int(cur.lastrowid)


def _update_run(db_path: str | Path, run_id: int, **values: Any) -> None:
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_runs
            SET opportunities_seen=?, orders_created=?, positions_opened=?, positions_settled=?, equity=?, raw_json=?
            WHERE id=?
            """,
            (
                values["opportunities_seen"],
                values["orders_created"],
                values["positions_opened"],
                values["positions_settled"],
                values["equity"],
                json.dumps(values.get("raw") or {}, sort_keys=True),
                run_id,
            ),
        )


def _has_position(db_path: str | Path, opportunity_id: str) -> bool:
    with closing_connection(db_path) as conn:
        return conn.execute("SELECT 1 FROM paper_positions WHERE opportunity_id=?", (opportunity_id,)).fetchone() is not None


def _open_position_count(db_path: str | Path) -> int:
    with closing_connection(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0])


def _strategy_open_notional(db_path: str | Path, strategy: str) -> float:
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT COALESCE(SUM(notional), 0) FROM paper_positions WHERE strategy=? AND status='open'", (strategy,)).fetchone()
    return float(row[0] or 0.0)


def _current_equity(db_path: str | Path, starting_bankroll: float) -> float:
    report = performance_report(db_path, starting_bankroll=starting_bankroll) if Path(db_path).exists() else {"equity": starting_bankroll}
    return float(report["equity"])


def _equity_values(db_path: str | Path, starting_bankroll: float) -> list[float]:
    if not Path(db_path).exists():
        return [starting_bankroll]
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT equity FROM paper_equity_curve ORDER BY id").fetchall()
    return [float(row["equity"]) for row in rows]


def _segment(rows: list[Any], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row[key] or "unknown")
        bucket = buckets.setdefault(label, {"open_positions": 0, "closed_positions": 0, "realized_pnl": 0.0, "unrealized_pnl": 0.0})
        if row["status"] == "closed":
            bucket["closed_positions"] += 1
            bucket["realized_pnl"] += float(row["realized_pnl"] or 0.0)
        else:
            bucket["open_positions"] += 1
            bucket["unrealized_pnl"] += float(row["unrealized_pnl"] or 0.0)
    for bucket in buckets.values():
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 6)
        bucket["unrealized_pnl"] = round(bucket["unrealized_pnl"], 6)
    return dict(sorted(buckets.items()))


def _max_drawdown(values: list[float]) -> float:
    peak = -math.inf
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value - peak)
    return drawdown


def _sharpe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    stdev = pstdev(values)
    return 0.0 if stdev == 0 else mean(values) / stdev * math.sqrt(len(values))


def _bounded_price(value: Any) -> float:
    price = _safe_float(value)
    if price <= 0:
        return 0.5
    return min(max(price, 0.01), 0.99)


def _stable_json(row: dict[str, Any]) -> str:
    payload = json.dumps(row, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _safe_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
