from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.paper_execution_simulator import PaperExecutionConfig
from src.analysis.paper_trading_engine import (
    DEFAULT_PAPER_TRADING_DB,
    DEFAULT_STARTING_BANKROLL,
    PaperTradingConfig,
    equity_report,
    init_paper_trading_db,
    open_positions_report,
    performance_report,
    run_paper_trading_engine,
)
from src.sqlite_utils import closing_connection

DEFAULT_PAPER_TRADING_LOG = "logs/paper_trading.log"


def run_paper_trading_service(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    log_path: str | Path = DEFAULT_PAPER_TRADING_LOG,
    limit: int = 25,
) -> dict[str, Any]:
    logger = _structured_logger(log_path)
    started = _utc_now()
    try:
        config = PaperTradingConfig.from_env()
        config.opportunity_limit = limit
        config.execution = PaperExecutionConfig.from_env()
        result = run_paper_trading_engine(db_path=db_path, config=config)
        health = paper_trading_health(db_path=db_path)
        payload = {
            "accepted": True,
            "started_at": started,
            "finished_at": _utc_now(),
            "result": result,
            "health": health,
        }
        _log_json(
            logger,
            "paper_trading_service_run",
            opportunities=result.get("opportunities_seen", 0),
            entries=result.get("positions_opened", 0),
            exits=result.get("positions_settled", 0),
            pnl=result.get("realized_pnl", 0),
            equity=result.get("equity", DEFAULT_STARTING_BANKROLL),
            db_path=str(db_path),
        )
        return payload
    except Exception as exc:
        _log_json(logger, "paper_trading_service_error", error=str(exc), db_path=str(db_path))
        return {"accepted": False, "started_at": started, "finished_at": _utc_now(), "error": str(exc)}


def paper_trading_health(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    perf = performance_report(db_path=db_path)
    equity = float(perf.get("equity") or DEFAULT_STARTING_BANKROLL)
    with closing_connection(db_path) as conn:
        last = conn.execute("SELECT run_timestamp FROM paper_runs ORDER BY id DESC LIMIT 1").fetchone()
        cutoff = (now - timedelta(hours=24)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        runs_24h = int(conn.execute("SELECT COUNT(*) FROM paper_runs WHERE run_timestamp >= ?", (cutoff,)).fetchone()[0])
    last_run = str(last["run_timestamp"]) if last else None
    status = "healthy" if last_run and runs_24h > 0 else "idle"
    return {
        "last_run": last_run,
        "runs_24h": runs_24h,
        "positions_open": int(perf.get("open_positions") or 0),
        "positions_closed": int(perf.get("closed_positions") or 0),
        "equity": round(equity, 6),
        "roi": round(float(perf.get("roi") or 0.0), 6),
        "status": status,
    }


def load_paper_trading_summary(db_path: str | Path = DEFAULT_PAPER_TRADING_DB) -> dict[str, Any]:
    perf = performance_report(db_path=db_path)
    health = paper_trading_health(db_path=db_path)
    return {
        "health": health,
        "performance": perf,
        "open_positions": open_positions_report(db_path=db_path).get("count", 0),
    }


def load_paper_trading_equity_curve(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = equity_report(db_path=db_path).get("equity_curve", [])
    if limit is not None:
        rows = rows[-max(int(limit), 0):]
    return rows


def load_recent_paper_trades(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    limit: int = 25,
) -> list[dict[str, Any]]:
    init_paper_trading_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                p.paper_position_id,
                p.opportunity_id,
                p.strategy,
                p.market_id,
                p.title,
                p.asset,
                p.side,
                p.entry_timestamp,
                p.entry_price,
                p.notional,
                p.status,
                p.exit_timestamp,
                p.exit_price,
                p.realized_pnl,
                p.unrealized_pnl,
                p.roi
            FROM paper_positions p
            ORDER BY p.paper_position_id DESC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _structured_logger(log_path: str | Path) -> logging.Logger:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("polylens.paper_trading_service")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target = str(path)
    if not any(isinstance(handler, logging.FileHandler) and handler.baseFilename == str(path.resolve()) for handler in logger.handlers):
        handler = logging.FileHandler(target)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _log_json(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {"timestamp": _utc_now(), "event": event, **fields}
    logger.info(json.dumps(payload, sort_keys=True))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
