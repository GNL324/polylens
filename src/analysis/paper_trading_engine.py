from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.analysis.opportunity_feed import get_paper_trading_opportunities
from src.analysis.paper_execution_simulator import (
    ExecutionResult,
    PaperExecutionConfig,
    simulate_entry,
    simulate_exit,
)
from src.sqlite_utils import closing_connection


DEFAULT_PAPER_TRADING_DB = "data/paper_trading.db"
DEFAULT_STARTING_BANKROLL = 100.0
DEFAULT_RISK_PER_TRADE = 0.02
DEFAULT_MAX_OPEN_POSITIONS = 10
DEFAULT_MAX_STRATEGY_EXPOSURE = 0.20


@dataclass
class PaperTradingConfig:
    """Paper-only portfolio risk controls.

    Percentage limits accept a fraction (``0.20`` is 20% of equity); values over
    one are treated as an absolute dollar cap so small simulations stay ergonomic.
    """

    starting_bankroll: float = DEFAULT_STARTING_BANKROLL
    sizing_mode: str = "percent_equity"  # fixed_dollar | percent_equity | kelly
    fixed_dollar_size: float = 5.0
    percent_of_equity: float | None = None
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE  # backwards-compatible percent alias
    kelly_fraction: float | None = None
    kelly_cap: float = 0.25
    max_concurrent_positions: int | None = None
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS
    max_exposure_per_market: float = 0.20
    max_exposure_per_category: float = 1.0
    max_strategy_exposure: float = DEFAULT_MAX_STRATEGY_EXPOSURE
    daily_loss_limit: float | None = None
    daily_trade_limit: int | None = None
    opportunity_limit: int = 25
    execution: PaperExecutionConfig | None = None

    def resolved_execution(self) -> PaperExecutionConfig:
        return self.execution or PaperExecutionConfig.zero_friction()

    @classmethod
    def from_env(cls) -> "PaperTradingConfig":
        """Load optional paper-only risk controls without enabling execution."""
        def number(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
        def integer(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
        def optional_number(name: str) -> float | None:
            value = os.environ.get(name)
            return None if value in (None, "") else number(name, 0.0)
        def optional_integer(name: str) -> int | None:
            value = os.environ.get(name)
            return None if value in (None, "") else integer(name, 0)
        return cls(
            starting_bankroll=number("POLYLENS_PAPER_STARTING_BANKROLL", DEFAULT_STARTING_BANKROLL),
            sizing_mode=str(os.environ.get("POLYLENS_PAPER_SIZING_MODE", "percent_equity")),
            fixed_dollar_size=number("POLYLENS_PAPER_FIXED_DOLLAR_SIZE", 5.0),
            percent_of_equity=optional_number("POLYLENS_PAPER_PERCENT_OF_EQUITY"),
            risk_per_trade=number("POLYLENS_PAPER_RISK_PER_TRADE", DEFAULT_RISK_PER_TRADE),
            kelly_fraction=optional_number("POLYLENS_PAPER_KELLY_FRACTION"),
            kelly_cap=number("POLYLENS_PAPER_KELLY_CAP", 0.25),
            max_concurrent_positions=optional_integer("POLYLENS_PAPER_MAX_CONCURRENT_POSITIONS"),
            max_open_positions=integer("POLYLENS_PAPER_MAX_OPEN_POSITIONS", DEFAULT_MAX_OPEN_POSITIONS),
            max_exposure_per_market=number("POLYLENS_PAPER_MAX_EXPOSURE_PER_MARKET", 0.20),
            max_exposure_per_category=number("POLYLENS_PAPER_MAX_EXPOSURE_PER_CATEGORY", 1.0),
            max_strategy_exposure=number("POLYLENS_PAPER_MAX_STRATEGY_EXPOSURE", DEFAULT_MAX_STRATEGY_EXPOSURE),
            daily_loss_limit=optional_number("POLYLENS_PAPER_DAILY_LOSS_LIMIT"),
            daily_trade_limit=optional_integer("POLYLENS_PAPER_DAILY_TRADE_LIMIT"),
            opportunity_limit=integer("POLYLENS_PAPER_OPPORTUNITY_LIMIT", 25),
        )


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
    signal_family: str
    source_wallet: str | None
    confidence: float
    validation_score: float
    market_category: str
    market_liquidity: float
    time_to_expiry_seconds: float
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def init_paper_trading_db(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_timestamp TEXT NOT NULL,
                opportunities_seen INTEGER NOT NULL, orders_created INTEGER NOT NULL,
                positions_opened INTEGER NOT NULL, positions_settled INTEGER NOT NULL,
                equity REAL NOT NULL, raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL, strategy TEXT NOT NULL, side TEXT NOT NULL,
                market_id TEXT NOT NULL, title TEXT NOT NULL, asset TEXT NOT NULL,
                simulated_price REAL NOT NULL, stake REAL NOT NULL, status TEXT NOT NULL,
                raw_json TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES paper_runs(id)
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                paper_position_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL UNIQUE, strategy TEXT NOT NULL, market_id TEXT NOT NULL,
                title TEXT NOT NULL, asset TEXT NOT NULL, side TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL, entry_price REAL NOT NULL, shares REAL NOT NULL,
                notional REAL NOT NULL, status TEXT NOT NULL, current_price REAL NOT NULL,
                exit_timestamp TEXT, exit_price REAL, realized_pnl REAL, unrealized_pnl REAL NOT NULL DEFAULT 0, roi REAL
            );
            CREATE TABLE IF NOT EXISTS paper_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, paper_position_id INTEGER NOT NULL,
                exit_timestamp TEXT NOT NULL, exit_price REAL NOT NULL, pnl REAL NOT NULL, roi REAL NOT NULL,
                reason TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES paper_runs(id),
                FOREIGN KEY(paper_position_id) REFERENCES paper_positions(paper_position_id)
            );
            CREATE TABLE IF NOT EXISTS paper_equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, timestamp TEXT NOT NULL,
                equity REAL NOT NULL, realized_pnl REAL NOT NULL, unrealized_pnl REAL NOT NULL,
                open_positions INTEGER NOT NULL, drawdown REAL NOT NULL, canonical INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(run_id) REFERENCES paper_runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status);
            CREATE INDEX IF NOT EXISTS idx_paper_positions_strategy ON paper_positions(strategy, status);
            CREATE INDEX IF NOT EXISTS idx_paper_orders_run ON paper_orders(run_id);
            CREATE INDEX IF NOT EXISTS idx_paper_settlements_position ON paper_settlements(paper_position_id);
            CREATE INDEX IF NOT EXISTS idx_paper_equity_run ON paper_equity_curve(run_id);
            """
        )
    from src.analysis.paper_portfolio import migrate_paper_portfolio

    migrate_paper_portfolio(db_path)


def collect_opportunities(limit: int = 25) -> list[dict[str, Any]]:
    return get_paper_trading_opportunities(limit=limit)


def run_paper_trading_engine(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    config: PaperTradingConfig | None = None,
    collectors: list[Callable[[], list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    config = config or PaperTradingConfig.from_env()
    execution = config.resolved_execution()
    raw = _collect_all(collectors, config.opportunity_limit)
    opportunities = sorted((_normalize_opportunity(item) for item in raw), key=lambda item: (-item.ranking_score, item.opportunity_id))
    run_id = _create_run(db_path, opportunities_seen=len(opportunities), equity=_current_equity(db_path, config.starting_bankroll))
    settled = settle_open_positions(db_path=db_path, run_id=run_id, execution=execution)
    orders_created = positions_opened = 0
    for opportunity in opportunities:
        if _has_position(db_path, opportunity.opportunity_id):
            continue
        requested_stake = calculate_position_size(db_path=db_path, strategy=opportunity.strategy, config=config, opportunity=opportunity)
        reason = _entry_block_reason(db_path, opportunity, requested_stake, config)
        if reason:
            _save_blocked_order(db_path, run_id, opportunity, reason)
            continue
        fill = simulate_entry(
            row=opportunity.raw,
            side=opportunity.side,
            requested_stake=requested_stake,
            config=execution,
        )
        if fill.status == "rejected":
            _save_blocked_order(db_path, run_id, opportunity, fill.reason or "execution_rejected")
            continue
        stake = fill.filled_stake
        if stake <= 0:
            _save_blocked_order(db_path, run_id, opportunity, fill.reason or "zero_fill")
            continue
        filled_opportunity = PaperOpportunity(
            opportunity_id=opportunity.opportunity_id,
            strategy=opportunity.strategy,
            market_id=opportunity.market_id,
            title=opportunity.title,
            asset=opportunity.asset,
            side=opportunity.side,
            entry_price=float(fill.fill_price or opportunity.entry_price),
            target_price=opportunity.target_price,
            estimated_roi=opportunity.estimated_roi,
            ranking_score=opportunity.ranking_score,
            signal_family=opportunity.signal_family,
            source_wallet=opportunity.source_wallet,
            confidence=opportunity.confidence,
            validation_score=opportunity.validation_score,
            market_category=opportunity.market_category,
            market_liquidity=opportunity.market_liquidity,
            time_to_expiry_seconds=opportunity.time_to_expiry_seconds,
            raw=_with_execution_metadata(opportunity.raw, fill),
        )
        order_id = _save_order(db_path, run_id, filled_opportunity, stake, execution=fill)
        if _open_position(db_path, order_id, filled_opportunity, stake):
            orders_created += 1
            positions_opened += 1
        else:
            _mark_order_blocked(db_path, order_id, "insufficient canonical paper cash")
    equity = record_equity_snapshot(db_path, run_id=run_id, starting_bankroll=config.starting_bankroll)
    _update_run(
        db_path, run_id, opportunities_seen=len(opportunities), orders_created=orders_created,
        positions_opened=positions_opened, positions_settled=settled, equity=equity["equity"],
        raw={"opportunity_ids": [item.opportunity_id for item in opportunities], "paper_only": True},
    )
    return {
        "run_id": run_id, "opportunities_seen": len(opportunities), "orders_created": orders_created,
        "positions_opened": positions_opened, "positions_settled": settled,
        "open_positions": _open_position_count(db_path), "equity": equity["equity"],
        "realized_pnl": equity["realized_pnl"], "unrealized_pnl": equity["unrealized_pnl"],
    }


def calculate_position_size(
    *,
    db_path: str | Path,
    strategy: str,
    config: PaperTradingConfig,
    opportunity: PaperOpportunity | None = None,
) -> float:
    """Size one simulated entry before portfolio exposure checks are applied."""
    from src.analysis.paper_portfolio import portfolio_report

    portfolio = portfolio_report(db_path, starting_bankroll=config.starting_bankroll)
    equity = max(0.0, float(portfolio["total_equity"]))
    mode = str(config.sizing_mode or "percent_equity").lower()
    if mode == "fixed_dollar":
        base = max(0.0, float(config.fixed_dollar_size))
    elif mode == "kelly":
        probability = _bounded_probability(opportunity.confidence if opportunity else 0.0)
        payoff = max(0.0, float(opportunity.estimated_roi if opportunity else 0.0))
        raw_kelly = probability - ((1.0 - probability) / payoff) if payoff > 0 else 0.0
        multiplier = 1.0 if config.kelly_fraction is None else max(0.0, float(config.kelly_fraction))
        base = equity * max(0.0, min(raw_kelly * multiplier, max(0.0, config.kelly_cap)))
    else:
        fraction = config.percent_of_equity if config.percent_of_equity is not None else config.risk_per_trade
        base = equity * max(0.0, float(fraction))
    strategy_capacity = _limit_dollars(config.max_strategy_exposure, equity) - _strategy_open_notional(db_path, strategy)
    return round(max(0.0, min(base, float(portfolio["available_cash"]), max(0.0, strategy_capacity))), 6)


def settle_open_positions(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    run_id: int | None = None,
    prices: dict[str, float] | None = None,
    execution: PaperExecutionConfig | None = None,
) -> int:
    from src.analysis.paper_portfolio import transition_position

    init_paper_trading_db(db_path)
    prices = prices or {}
    execution = execution or PaperExecutionConfig.zero_friction()
    settled = 0
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE status IN ('pending','open','partially_exited') ORDER BY paper_position_id").fetchall()
    for row in rows:
        with closing_connection(db_path) as conn:
            order = conn.execute("SELECT raw_json FROM paper_orders WHERE id=?", (row["order_id"],)).fetchone()
        raw = _json(order["raw_json"] if order else "{}")
        exit_price = _safe_float(prices.get(str(row["opportunity_id"])))
        if exit_price <= 0:
            exit_price = _safe_float(raw.get("settlement_price") or raw.get("exit_price") or raw.get("target_price"))
        if exit_price <= 0:
            mark = _safe_float(prices.get(str(row["opportunity_id"]))) or _safe_float(row["current_price"])
            with closing_connection(db_path) as conn:
                conn.execute(
                    "UPDATE paper_positions SET current_price=?, unrealized_pnl=?, updated_timestamp=? WHERE paper_position_id=?",
                    (mark, round((mark - _safe_float(row["entry_price"])) * _safe_float(row["shares"]), 6), _utc_now(), row["paper_position_id"]),
                )
            continue

        quote_row = {
            **raw,
            "exit_price": exit_price,
            "best_bid": exit_price,
            "yes_bid": exit_price,
            "entry_price": float(row["entry_price"]),
        }
        exit_result = simulate_exit(
            row=quote_row,
            side=str(row["side"] or "yes"),
            shares=float(row["shares"] or 0.0),
            config=execution,
        )
        if exit_result.status == "rejected":
            continue

        simulated_exit_price = exit_result.fill_price if exit_result.fill_price is not None else exit_price
        reason = "simulated_exit"
        if exit_result.reason:
            reason = f"simulated_exit:{exit_result.reason}"
        updated = transition_position(
            db_path, paper_position_id=int(row["paper_position_id"]), status="closed", price=simulated_exit_price,
            quantity=_safe_float(row["shares"]), reason=reason,
        )
        with closing_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id or 0, row["paper_position_id"], updated["closed_at"] or _utc_now(), simulated_exit_price, updated["realized_pnl"], _safe_float(updated.get("roi")), reason),
            )
        settled += 1
    return settled


def performance_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB, starting_bankroll: float = DEFAULT_STARTING_BANKROLL) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    from src.analysis.paper_portfolio import portfolio_report

    return portfolio_report(db_path, starting_bankroll=starting_bankroll)


def open_positions_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> dict[str, Any]:
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM paper_positions WHERE status IN ('pending','open','partially_exited') ORDER BY paper_position_id").fetchall()
    return {"open_positions": [_row_dict(row) for row in rows], "count": len(rows)}


def equity_report(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> dict[str, Any]:
    report = performance_report(db_path)
    curve = report["equity_curve"]
    return {"equity_curve": curve, "latest_equity": report["equity"], "max_drawdown": report["max_drawdown"]}


def record_equity_snapshot(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    run_id: int,
    starting_bankroll: float = DEFAULT_STARTING_BANKROLL,
    timestamp: str | None = None,
) -> dict[str, float]:
    report = performance_report(db_path, starting_bankroll=starting_bankroll)
    with closing_connection(db_path) as conn:
        prior = [float(row[0]) for row in conn.execute("SELECT equity FROM paper_equity_curve ORDER BY id").fetchall()]
        peak = max([starting_bankroll, *prior, float(report["equity"])])
        drawdown = round(float(report["equity"]) - peak, 6)
        conn.execute(
            "INSERT INTO paper_equity_curve (run_id, timestamp, equity, realized_pnl, unrealized_pnl, open_positions, drawdown) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, timestamp or _utc_now(), report["equity"], report["realized_pnl"], report["unrealized_pnl"], report["open_positions"], drawdown),
        )
    return {"equity": report["equity"], "realized_pnl": report["realized_pnl"], "unrealized_pnl": report["unrealized_pnl"], "drawdown": drawdown}


def _entry_block_reason(db_path: str | Path, opportunity: PaperOpportunity, stake: float, config: PaperTradingConfig) -> str | None:
    if stake <= 0:
        return "insufficient paper cash or strategy capacity"
    effective_max = config.max_concurrent_positions if config.max_concurrent_positions is not None else config.max_open_positions
    if _open_position_count(db_path) >= max(0, int(effective_max)):
        return "max concurrent position limit reached"
    equity = _current_equity(db_path, config.starting_bankroll)
    if _market_open_notional(db_path, opportunity.market_id) + stake > _limit_dollars(config.max_exposure_per_market, equity) + 1e-6:
        return "max exposure per market exceeded"
    if _category_open_notional(db_path, opportunity.market_category) + stake > _limit_dollars(config.max_exposure_per_category, equity) + 1e-6:
        return "max exposure per category exceeded"
    if config.daily_trade_limit is not None and _daily_filled_trade_count(db_path) >= max(0, int(config.daily_trade_limit)):
        return "daily trade limit reached"
    if config.daily_loss_limit is not None and _daily_realized_loss(db_path) >= max(0.0, float(config.daily_loss_limit)):
        return "daily loss limit reached"
    return None


def _open_position(db_path: str | Path, order_id: int, opportunity: PaperOpportunity, stake: float) -> bool:
    from src.analysis.paper_portfolio import record_position_transition

    if stake <= 0 or stake > performance_report(db_path)["available_cash"] + 1e-6:
        return False
    shares = stake / opportunity.entry_price if opportunity.entry_price > 0 else 0.0
    if shares <= 0:
        return False
    timestamp = _utc_now()
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_positions
            (order_id, opportunity_id, strategy, market_id, title, asset, side, entry_timestamp, entry_price, shares, notional, status, current_price,
             signal_family, source_wallet, reason, confidence, validation_score, market_category, market_liquidity, time_to_expiry_seconds,
             created_timestamp, updated_timestamp, pending_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, opportunity.opportunity_id, opportunity.strategy, opportunity.market_id, opportunity.title, opportunity.asset, opportunity.side,
                timestamp, opportunity.entry_price, round(shares, 8), round(stake, 6), opportunity.entry_price,
                opportunity.signal_family, opportunity.source_wallet, str(opportunity.raw.get("reason") or opportunity.raw.get("recommendation_reason") or "") or None,
                opportunity.confidence, opportunity.validation_score, opportunity.market_category, opportunity.market_liquidity, opportunity.time_to_expiry_seconds,
                timestamp, timestamp, timestamp,
            ),
        )
        if cur.rowcount <= 0:
            return False
        position_id = int(cur.lastrowid)
        conn.execute("UPDATE paper_positions SET status='open', open_timestamp=?, updated_timestamp=? WHERE paper_position_id=?", (timestamp, timestamp, position_id))
    record_position_transition(db_path, paper_position_id=position_id, order_id=order_id, from_status=None, to_status="pending", timestamp=timestamp, reason="paper order accepted", quantity=shares, price=opportunity.entry_price)
    record_position_transition(db_path, paper_position_id=position_id, order_id=order_id, from_status="pending", to_status="open", timestamp=timestamp, reason="simulated fill", quantity=shares, price=opportunity.entry_price)
    return True


def _save_order(
    db_path: str | Path,
    run_id: int,
    opportunity: PaperOpportunity,
    stake: float,
    *,
    execution: ExecutionResult | None = None,
) -> int:
    timestamp = _utc_now()
    raw_payload = _with_execution_metadata(opportunity.raw or {}, execution) if execution else dict(opportunity.raw or {})
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO paper_orders
            (run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json, created_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?)""",
            (run_id, opportunity.opportunity_id, opportunity.strategy, opportunity.side, opportunity.market_id, opportunity.title, opportunity.asset,
             opportunity.entry_price, stake, json.dumps(raw_payload, sort_keys=True), timestamp),
        )
        return int(cur.lastrowid)


def _with_execution_metadata(raw: dict[str, Any], execution: ExecutionResult) -> dict[str, Any]:
    payload = dict(raw)
    payload["execution"] = execution.to_dict()
    return payload


def _save_blocked_order(db_path: str | Path, run_id: int, opportunity: PaperOpportunity, reason: str) -> None:
    from src.analysis.paper_portfolio import record_position_transition

    timestamp = _utc_now()
    raw = {**opportunity.raw, "blocked_reason": reason}
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO paper_orders
            (run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json, created_timestamp, blocked_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'blocked', ?, ?, ?)""",
            (run_id, opportunity.opportunity_id, opportunity.strategy, opportunity.side, opportunity.market_id, opportunity.title, opportunity.asset,
             opportunity.entry_price, json.dumps(raw, sort_keys=True), timestamp, reason),
        )
        order_id = int(cur.lastrowid)
    record_position_transition(db_path, paper_position_id=None, order_id=order_id, from_status=None, to_status="blocked", timestamp=timestamp, reason=reason)


def _mark_order_blocked(db_path: str | Path, order_id: int, reason: str) -> None:
    with closing_connection(db_path) as conn:
        conn.execute("UPDATE paper_orders SET status='blocked', blocked_reason=? WHERE id=?", (reason, order_id))


def _create_run(db_path: str | Path, *, opportunities_seen: int, equity: float) -> int:
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO paper_runs (run_timestamp, opportunities_seen, orders_created, positions_opened, positions_settled, equity, raw_json) VALUES (?, ?, 0, 0, 0, ?, '{}')",
            (_utc_now(), opportunities_seen, equity),
        )
        return int(cur.lastrowid)


def _update_run(db_path: str | Path, run_id: int, **values: Any) -> None:
    with closing_connection(db_path) as conn:
        conn.execute(
            "UPDATE paper_runs SET opportunities_seen=?, orders_created=?, positions_opened=?, positions_settled=?, equity=?, raw_json=? WHERE id=?",
            (values["opportunities_seen"], values["orders_created"], values["positions_opened"], values["positions_settled"], values["equity"], json.dumps(values.get("raw") or {}, sort_keys=True), run_id),
        )


def _collect_all(collectors: list[Callable[[], list[dict[str, Any]]]] | None, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for collector in collectors or [lambda: collect_opportunities(limit=limit)]:
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
    expiry_seconds = _safe_float(row.get("time_to_expiry_seconds") or row.get("seconds_to_expiry"))
    if not expiry_seconds and row.get("expires_at"):
        try:
            expiry_seconds = max(0.0, (datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            expiry_seconds = 0.0
    return PaperOpportunity(
        opportunity_id=opportunity_id, strategy=strategy, market_id=str(row.get("market_id") or row.get("ticker") or row.get("id") or opportunity_id),
        title=str(row.get("title") or row.get("market_title") or row.get("polymarket_title") or row.get("player") or "unknown"),
        asset=str(row.get("asset") or row.get("sport") or row.get("venue") or "OTHER").upper(), side=str(row.get("side") or row.get("direction") or row.get("outcome") or "yes").lower(),
        entry_price=entry_price, target_price=target if target > 0 else None, estimated_roi=estimated_roi,
        ranking_score=_safe_float(row.get("ranking_score") or row.get("score")),
        signal_family=str(row.get("signal_family") or row.get("signal_type") or strategy),
        source_wallet=str(row.get("source_wallet") or row.get("wallet") or row.get("trader_address") or "") or None,
        confidence=_bounded_probability(row.get("confidence") or row.get("probability")), validation_score=_safe_float(row.get("validation_score") or row.get("validation")),
        market_category=str(row.get("market_category") or row.get("category") or row.get("asset") or "unknown"),
        market_liquidity=_safe_float(row.get("market_liquidity") or row.get("liquidity")), time_to_expiry_seconds=expiry_seconds, raw=dict(row),
    )


def _has_position(db_path: str | Path, opportunity_id: str) -> bool:
    with closing_connection(db_path) as conn:
        return conn.execute("SELECT 1 FROM paper_positions WHERE opportunity_id=?", (opportunity_id,)).fetchone() is not None


def _open_position_count(db_path: str | Path) -> int:
    with closing_connection(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM paper_positions WHERE status IN ('pending','open','partially_exited')").fetchone()[0])


def _open_notional(db_path: str | Path, column: str, value: str) -> float:
    if column not in {"strategy", "market_id", "market_category"}:
        raise ValueError("unsupported paper exposure field")
    with closing_connection(db_path) as conn:
        row = conn.execute(f"SELECT COALESCE(SUM(notional), 0) FROM paper_positions WHERE {column}=? AND status IN ('pending','open','partially_exited')", (value,)).fetchone()
    return _safe_float(row[0])


def _strategy_open_notional(db_path: str | Path, strategy: str) -> float:
    return _open_notional(db_path, "strategy", strategy)


def _market_open_notional(db_path: str | Path, market_id: str) -> float:
    return _open_notional(db_path, "market_id", market_id)


def _category_open_notional(db_path: str | Path, category: str) -> float:
    return _open_notional(db_path, "market_category", category)


def _daily_filled_trade_count(db_path: str | Path) -> int:
    today = _utc_now()[:10]
    with closing_connection(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM paper_orders WHERE status='filled' AND substr(COALESCE(created_timestamp, ''), 1, 10)=?", (today,)).fetchone()[0])


def _daily_realized_loss(db_path: str | Path) -> float:
    today = _utc_now()[:10]
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN -realized_pnl ELSE 0 END), 0) FROM paper_positions WHERE status IN ('closed','expired') AND substr(COALESCE(exit_timestamp, ''), 1, 10)=?", (today,)).fetchone()
    return _safe_float(row[0])


def _current_equity(db_path: str | Path, starting_bankroll: float) -> float:
    return float(performance_report(db_path, starting_bankroll=starting_bankroll)["equity"])


def _limit_dollars(limit: float, equity: float) -> float:
    limit = max(0.0, float(limit))
    return equity * limit if limit <= 1.0 else limit


def _bounded_probability(value: Any) -> float:
    value = _safe_float(value)
    if value > 1.0:
        value /= 100.0
    return min(1.0, max(0.0, value))


def _bounded_price(value: Any) -> float:
    return min(0.99, max(0.01, _safe_float(value) or 0.5))


def _stable_json(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _json(value: Any) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
