from __future__ import annotations

from pathlib import Path
from typing import Any

from src.services.results_service import fetch_trades
from src.web.db import POLYLENS_DB_PATH, connect, load_json, table_exists
from src.web.redaction import redact_mapping


def risk_payload(db_path: str | Path = POLYLENS_DB_PATH, bankroll: float = 1000.0) -> dict[str, Any]:
    from src.risk import RiskEngine

    engine = RiskEngine(db_path)
    status = redact_mapping(engine.status())
    trades = fetch_trades(db_path)
    return {
        "bankroll": bankroll,
        "live_trading_disabled": True,
        "trade_allowed": False,
        "trade_allowed_reason": "paper mode only; live trading is disabled in the web dashboard",
        "exposure_by_platform": status.get("exposure_by_venue", {}),
        "exposure_by_market_type": _sum_open_exposure(trades, "market_type"),
        "exposure_by_strategy": _sum_open_exposure(trades, "strategy"),
        "stale_data_warnings": stale_data_warnings(db_path),
        "missing_hedge_warnings": missing_hedge_warnings(db_path),
        "risk_status": status,
    }


def stale_data_warnings(db_path: str | Path = POLYLENS_DB_PATH) -> list[str]:
    with connect(db_path) as conn:
        warnings: list[str] = []
        for table, column in (("scan_runs", "timestamp"), ("opportunities", "timestamp")):
            if not table_exists(conn, table):
                warnings.append(f"{table}: no data yet")
                continue
            row = conn.execute(f"SELECT MAX({column}) AS latest FROM {table}").fetchone()
            if not row or not row["latest"]:
                warnings.append(f"{table}: no data yet")
        return warnings


def missing_hedge_warnings(db_path: str | Path = POLYLENS_DB_PATH, limit: int = 20) -> list[str]:
    warnings: list[str] = []
    with connect(db_path) as conn:
        if not table_exists(conn, "opportunities"):
            return ["opportunities: no data yet"]
        rows = conn.execute("SELECT id, candidate_json FROM opportunities ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    for row in rows:
        candidate = load_json(row["candidate_json"], {})
        if candidate and not (candidate.get("hedge_legs") or candidate.get("kalshi_ticker") and candidate.get("polymarket_id")):
            warnings.append(f"opportunity {row['id']}: hedge leg not confirmed")
    return warnings


def _sum_open_exposure(trades: list[dict[str, Any]], key: str) -> dict[str, float]:
    exposure: dict[str, float] = {}
    for trade in trades:
        status = str(trade.get("status") or "").lower()
        if status in {"closed", "settled", "resolved"}:
            continue
        name = str(trade.get(key) or "unknown")
        exposure[name] = round(exposure.get(name, 0.0) + abs(_float(trade.get("stake"))), 6)
    return exposure


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0

