from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB
from src.analysis.short_crypto_paper import DEFAULT_DB_PATH as DEFAULT_SHORT_CRYPTO_PAPER_DB

DEFAULT_STRATEGY_FAMILIES = (
    "early_entry",
    "conviction",
    "contrarian",
    "consensus",
    "exit",
    "BTC 5m momentum",
)


@dataclass(frozen=True)
class _PaperRow:
    source: str
    identifier: str
    strategy: str
    market_title: str
    opened_at: str
    closed_at: str | None
    status: str
    realized_pnl: float
    unrealized_pnl: float
    notional: float
    wallet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        pnl = self.realized_pnl if self.status != "OPEN" else self.unrealized_pnl
        return {
            "source": self.source,
            "id": self.identifier,
            "strategy": self.strategy,
            "market_title": self.market_title,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "status": self.status,
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "pnl": round(pnl, 6),
            "notional": round(self.notional, 6),
            "wallet": self.wallet,
        }


def paper_trading_intelligence(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    short_crypto_db_path: str | Path = DEFAULT_SHORT_CRYPTO_PAPER_DB,
    now: datetime | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    now = _as_utc(now or datetime.now(timezone.utc))
    rows: list[_PaperRow] = []
    fills: list[dict[str, Any]] = []
    warnings: list[str] = []

    standard_rows, standard_fills, standard_warnings = _read_standard_paper_db(Path(db_path))
    short_rows, short_fills, short_warnings = _read_short_crypto_db(Path(short_crypto_db_path))
    rows.extend(standard_rows)
    rows.extend(short_rows)
    fills.extend(standard_fills)
    fills.extend(short_fills)
    warnings.extend(standard_warnings)
    warnings.extend(short_warnings)

    open_rows = [row for row in rows if row.status == "OPEN"]
    closed_rows = [row for row in rows if row.status != "OPEN"]
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    daily_pnl = sum(row.realized_pnl for row in closed_rows if _timestamp_at_or_after(row.closed_at, today_start))
    pnl_7d = sum(row.realized_pnl for row in closed_rows if _timestamp_at_or_after(row.closed_at, seven_days_ago))
    total_pnl = sum(row.realized_pnl for row in closed_rows)
    wins = sum(1 for row in closed_rows if row.realized_pnl > 0)
    strategy_breakdown = _strategy_breakdown(rows)
    ranked = [item for item in strategy_breakdown.values() if item["trade_count"] > 0]
    top = max(ranked, key=lambda item: (item["realized_pnl"] + item["unrealized_pnl"], item["win_rate"], item["strategy"]), default=None)
    worst = min(ranked, key=lambda item: (item["realized_pnl"] + item["unrealized_pnl"], item["win_rate"], item["strategy"]), default=None)

    recent = sorted(rows, key=lambda row: _sort_timestamp(row.closed_at or row.opened_at), reverse=True)
    recent_fills = sorted(fills, key=lambda row: _sort_timestamp(row.get("filled_at")), reverse=True)
    return {
        "read_only": True,
        "paper_only": True,
        "recent_trades": [row.to_dict() for row in recent[:limit]],
        "recent_fills": recent_fills[:limit],
        "open_positions": [row.to_dict() for row in sorted(open_rows, key=lambda row: _sort_timestamp(row.opened_at), reverse=True)[:limit]],
        "closed_positions": [row.to_dict() for row in sorted(closed_rows, key=lambda row: _sort_timestamp(row.closed_at), reverse=True)[:limit]],
        "daily_pnl": round(daily_pnl, 6),
        "pnl_7d": round(pnl_7d, 6),
        "total_pnl": round(total_pnl, 6),
        "win_rate": round(wins / len(closed_rows), 6) if closed_rows else 0.0,
        "trade_count": len(rows),
        "open_positions_count": len(open_rows),
        "closed_positions_count": len(closed_rows),
        "strategy_breakdown": strategy_breakdown,
        "top_strategy": top,
        "worst_strategy": worst,
        "warnings": warnings,
    }


def _read_standard_paper_db(path: Path) -> tuple[list[_PaperRow], list[dict[str, Any]], list[str]]:
    rows: list[_PaperRow] = []
    fills: list[dict[str, Any]] = []
    warnings: list[str] = []
    with _readonly_connection(path, warnings) as conn:
        if conn is None:
            return rows, fills, warnings
        if _has_table(conn, "paper_positions"):
            raw_by_order_id = _paper_order_raw_by_id(conn) if _has_table(conn, "paper_orders") else {}
            for row in conn.execute("SELECT * FROM paper_positions ORDER BY paper_position_id DESC").fetchall():
                status = str(row["status"] or "").lower()
                realized = _float(row["realized_pnl"])
                raw = raw_by_order_id.get(int(row["order_id"] or 0), {})
                rows.append(
                    _PaperRow(
                        source="paper_trading",
                        identifier=str(row["paper_position_id"]),
                        strategy=_strategy_name(row["strategy"]),
                        market_title=str(row["title"] or row["market_id"] or "unknown"),
                        opened_at=str(row["entry_timestamp"] or ""),
                        closed_at=str(row["exit_timestamp"] or "") or None,
                        status="OPEN" if status == "open" else _closed_status(realized),
                        realized_pnl=realized,
                        unrealized_pnl=_float(row["unrealized_pnl"]),
                        notional=_float(row["notional"]),
                        wallet=_extract_wallet(raw),
                    )
                )
        if _has_table(conn, "paper_orders"):
            for row in conn.execute("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 25").fetchall():
                fills.append(
                    {
                        "source": "paper_trading",
                        "id": str(row["id"]),
                        "strategy": _strategy_name(row["strategy"]),
                        "market_title": str(row["title"] or row["market_id"] or "unknown"),
                        "filled_at": "",
                        "price": _float(row["simulated_price"]),
                        "size": _float(row["stake"]),
                        "status": str(row["status"] or "filled").upper(),
                    }
                )
    return rows, fills, warnings


def _read_short_crypto_db(path: Path) -> tuple[list[_PaperRow], list[dict[str, Any]], list[str]]:
    rows: list[_PaperRow] = []
    fills: list[dict[str, Any]] = []
    warnings: list[str] = []
    with _readonly_connection(path, warnings) as conn:
        if conn is None:
            return rows, fills, warnings
        if not _has_table(conn, "paper_trades"):
            return rows, fills, warnings
        settlement_by_trade: dict[int, Any] = {}
        if _has_table(conn, "paper_settlements"):
            settlement_by_trade = {int(row["trade_id"]): row for row in conn.execute("SELECT * FROM paper_settlements").fetchall()}
        for row in conn.execute("SELECT * FROM paper_trades ORDER BY id DESC").fetchall():
            trade_id = int(row["id"])
            settlement = settlement_by_trade.get(trade_id)
            pnl = _float(settlement["pnl"]) if settlement is not None else 0.0
            strategy = _strategy_name(row["strategy_label"] or "short_crypto_paper")
            raw = _loads(row["raw_json"])
            status = "OPEN" if settlement is None and str(row["status"] or "").lower() == "open" else _closed_status(pnl)
            rows.append(
                _PaperRow(
                    source="short_crypto_paper",
                    identifier=str(trade_id),
                    strategy=strategy,
                    market_title=str(row["market"] or row["ticker"] or row["slug"] or "short crypto"),
                    opened_at=str(row["created_at"] or row["entry_time"] or ""),
                    closed_at=str(settlement["settled_at"] or "") if settlement is not None else None,
                    status=status,
                    realized_pnl=pnl if status != "OPEN" else 0.0,
                    unrealized_pnl=0.0,
                    notional=_float(row["paper_cost"]),
                    wallet=_extract_wallet(raw),
                )
            )
            fills.append(
                {
                    "source": "short_crypto_paper",
                    "id": str(trade_id),
                    "strategy": strategy,
                    "market_title": str(row["market"] or row["ticker"] or row["slug"] or "short crypto"),
                    "filled_at": str(row["created_at"] or row["entry_time"] or ""),
                    "price": _float(row["entry_price"]),
                    "size": _float(row["size"]),
                    "status": str(row["status"] or "open").upper(),
                }
            )
    return rows, fills, warnings


def _strategy_breakdown(rows: Iterable[_PaperRow]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {
        family: {
            "strategy": family,
            "trade_count": 0,
            "open_positions": 0,
            "closed_positions": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "win_rate": 0.0,
        }
        for family in DEFAULT_STRATEGY_FAMILIES
    }
    wins: dict[str, int] = {}
    for row in rows:
        bucket = buckets.setdefault(
            row.strategy,
            {
                "strategy": row.strategy,
                "trade_count": 0,
                "open_positions": 0,
                "closed_positions": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "win_rate": 0.0,
            },
        )
        bucket["trade_count"] += 1
        bucket["realized_pnl"] += row.realized_pnl
        bucket["unrealized_pnl"] += row.unrealized_pnl
        if row.status == "OPEN":
            bucket["open_positions"] += 1
        else:
            bucket["closed_positions"] += 1
            if row.realized_pnl > 0:
                wins[row.strategy] = wins.get(row.strategy, 0) + 1
    for strategy, bucket in buckets.items():
        closed = int(bucket["closed_positions"] or 0)
        bucket["realized_pnl"] = round(float(bucket["realized_pnl"]), 6)
        bucket["unrealized_pnl"] = round(float(bucket["unrealized_pnl"]), 6)
        bucket["win_rate"] = round(wins.get(strategy, 0) / closed, 6) if closed else 0.0
    return dict(sorted(buckets.items()))


class _readonly_connection:
    def __init__(self, path: Path, warnings: list[str]) -> None:
        self.path = path
        self.warnings = warnings
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        uri = f"file:{self.path}?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True)
            self.conn.row_factory = sqlite3.Row
            return self.conn
        except sqlite3.Error as exc:
            self.warnings.append(f"{self.path.name}: {exc}")
            return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.conn is not None:
            self.conn.close()


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
    return row is not None


def _paper_order_raw_by_id(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    raw_by_id: dict[int, dict[str, Any]] = {}
    for row in conn.execute("SELECT id, raw_json FROM paper_orders").fetchall():
        raw_by_id[int(row["id"])] = _loads(row["raw_json"])
    return raw_by_id


def _loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_wallet(payload: dict[str, Any]) -> str | None:
    for key in ("wallet", "wallet_address", "trader_wallet", "address", "maker", "proxy_wallet"):
        value = str(payload.get(key) or "").strip()
        if value.startswith("0x") and len(value) == 42:
            return value
    return None


def _closed_status(pnl: float) -> str:
    if pnl > 0:
        return "WIN"
    if pnl < 0:
        return "LOSS"
    return "FLAT"


def _strategy_name(value: Any) -> str:
    text = str(value or "unknown")
    lower = text.lower()
    if "btc_5m_momentum" in lower or "btc 5m momentum" in lower:
        return "BTC 5m momentum"
    for family in DEFAULT_STRATEGY_FAMILIES:
        if lower == family.lower():
            return family
    return text[:80]


def _timestamp_at_or_after(value: str | None, cutoff: datetime) -> bool:
    parsed = _parse_timestamp(value)
    return parsed is not None and parsed >= cutoff


def _sort_timestamp(value: Any) -> datetime:
    return _parse_timestamp(value) or datetime.min.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
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


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_legacy_paper_trading_intelligence = paper_trading_intelligence


def paper_trading_intelligence(
    *,
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    short_crypto_db_path: str | Path = DEFAULT_SHORT_CRYPTO_PAPER_DB,
    now: datetime | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    from src.analysis.paper_portfolio import portfolio_trade_log
    from src.analysis.paper_trading_engine import init_paper_trading_db, performance_report

    now = _as_utc(now or datetime.now(timezone.utc))
    init_paper_trading_db(db_path)
    base = _legacy_paper_trading_intelligence(
        db_path=db_path,
        short_crypto_db_path=short_crypto_db_path,
        now=now,
        limit=limit,
    )
    portfolio = performance_report(db_path)
    log = portfolio_trade_log(db_path, limit=max(int(limit), 1) * 5)
    open_rows = [row for row in log if row["status"] == "open"]
    closed_rows = [row for row in log if row["status"] == "closed"]
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    daily_pnl = sum(float(row["realized_pnl"] or 0.0) for row in closed_rows if _timestamp_at_or_after(row.get("closed_at"), today_start))
    pnl_7d = sum(float(row["realized_pnl"] or 0.0) for row in closed_rows if _timestamp_at_or_after(row.get("closed_at"), seven_days_ago))
    strategy_breakdown = portfolio["by_strategy"]
    ranked = [item for item in strategy_breakdown.values() if item["trade_count"]]
    top = max(ranked, key=lambda item: (item["realized_pnl"] + item["unrealized_pnl"], item["win_rate"], item["strategy"]), default=None)
    worst = min(ranked, key=lambda item: (item["realized_pnl"] + item["unrealized_pnl"], item["win_rate"], item["strategy"]), default=None)
    base.update(
        {
            "recent_trades": log[:limit],
            "open_positions": open_rows[:limit],
            "closed_positions": closed_rows[:limit],
            "daily_pnl": round(daily_pnl, 6),
            "pnl_7d": round(pnl_7d, 6),
            "total_pnl": portfolio["total_pnl"],
            "win_rate": portfolio["win_rate"],
            "trade_count": portfolio["trade_count"],
            "open_positions_count": portfolio["open_positions"],
            "closed_positions_count": portfolio["closed_positions"],
            "strategy_breakdown": strategy_breakdown,
            "top_strategy": top,
            "worst_strategy": worst,
            "starting_bankroll": portfolio["starting_bankroll"],
            "cash_balance": portfolio["cash_balance"],
            "open_position_value": portfolio["open_position_value"],
            "realized_pnl": portfolio["realized_pnl"],
            "unrealized_pnl": portfolio["unrealized_pnl"],
            "current_equity": portfolio["total_equity"],
            "total_equity": portfolio["total_equity"],
            "roi_pct": portfolio["roi_pct"],
            "exposure": portfolio.get("exposure", 0.0),
            "drawdown": portfolio.get("drawdown", 0.0),
            "max_drawdown": portfolio.get("max_drawdown", 0.0),
            "today_return": portfolio.get("today_return", 0.0),
            "weekly_return": portfolio.get("weekly_return", 0.0),
            "monthly_return": portfolio.get("monthly_return", 0.0),
            "best_wallet": portfolio.get("best_wallet"),
            "worst_wallet": portfolio.get("worst_wallet"),
            "by_signal_family": portfolio.get("by_signal_family", {}),
            "by_source_wallet": portfolio.get("by_source_wallet", {}),
            "by_market_category": portfolio.get("by_market_category", {}),
            "legacy_anomalous_count": portfolio["legacy_anomalous_count"],
        }
    )
    if portfolio["legacy_anomalous_count"]:
        base["warnings"] = [*base.get("warnings", []), f"{portfolio['legacy_anomalous_count']} legacy rows excluded from the $100 portfolio"]
    return base
