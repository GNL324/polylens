from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.web.db import POLYLENS_DB_PATH, connect, ensure_trades_table, table_exists


@dataclass(frozen=True)
class ResultsSummary:
    paper_realized_pnl: float = 0.0
    paper_unrealized_pnl: float = 0.0
    live_realized_pnl: float = 0.0
    live_unrealized_pnl: float = 0.0
    total_volume: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    average_roi: float = 0.0
    open_positions: int = 0
    closed_trades: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return self.__dict__.copy()


def initialize_results_store(db_path: str | Path = POLYLENS_DB_PATH) -> None:
    ensure_trades_table(db_path)


def fetch_trades(db_path: str | Path = POLYLENS_DB_PATH, limit: int = 100) -> list[dict[str, Any]]:
    initialize_results_store(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, mode, opened_at, closed_at, platform, market_id, market_title, strategy, market_type,
                   side, entry_price, exit_price, stake, quantity, gross_pnl, fees, net_pnl, roi, status, notes
            FROM trades
            ORDER BY COALESCE(closed_at, opened_at, '') DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def summarize_results(db_path: str | Path = POLYLENS_DB_PATH) -> dict[str, float | int]:
    initialize_results_store(db_path)
    with connect(db_path) as conn:
        if not table_exists(conn, "trades"):
            return ResultsSummary().to_dict()
        rows = [dict(row) for row in conn.execute("SELECT mode, stake, fees, net_pnl, roi, status FROM trades")]

    paper_realized = 0.0
    paper_unrealized = 0.0
    live_realized = 0.0
    live_unrealized = 0.0
    total_volume = 0.0
    fees = 0.0
    closed_trades = 0
    open_positions = 0
    winning_closed = 0
    roi_values: list[float] = []

    for row in rows:
        mode = str(row.get("mode") or "paper").strip().lower()
        status = str(row.get("status") or "").strip().lower()
        pnl = _float(row.get("net_pnl"))
        total_volume += abs(_float(row.get("stake")))
        fees += _float(row.get("fees"))
        if row.get("roi") is not None:
            roi_values.append(_float(row.get("roi")))

        is_closed = status in {"closed", "settled", "resolved"} or row.get("status") is None and row.get("net_pnl") is not None
        if is_closed:
            closed_trades += 1
            if pnl > 0:
                winning_closed += 1
            if mode == "live":
                live_realized += pnl
            else:
                paper_realized += pnl
        else:
            open_positions += 1
            if mode == "live":
                live_unrealized += pnl
            else:
                paper_unrealized += pnl

    net_pnl = paper_realized + paper_unrealized + live_realized + live_unrealized
    return ResultsSummary(
        paper_realized_pnl=round(paper_realized, 6),
        paper_unrealized_pnl=round(paper_unrealized, 6),
        live_realized_pnl=round(live_realized, 6),
        live_unrealized_pnl=round(live_unrealized, 6),
        total_volume=round(total_volume, 6),
        fees=round(fees, 6),
        net_pnl=round(net_pnl, 6),
        win_rate=round((winning_closed / closed_trades) if closed_trades else 0.0, 6),
        average_roi=round((sum(roi_values) / len(roi_values)) if roi_values else 0.0, 6),
        open_positions=open_positions,
        closed_trades=closed_trades,
    ).to_dict()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0

