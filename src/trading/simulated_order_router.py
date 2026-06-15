from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection
from src.trading.execution_gate import check_execution_gate

DEFAULT_READINESS_DB = "data/trading_readiness.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_simulated_order_db(db_path: str | Path = DEFAULT_READINESS_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS simulated_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                strategy_id TEXT,
                risk_decision_json TEXT NOT NULL DEFAULT '{}',
                gate_result_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                rejection_reason TEXT,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_simulated_orders_time ON simulated_orders(recorded_at DESC);
            """
        )


class SimulatedOrderRouter:
    """Dry-run order router that never places external orders."""

    def __init__(self, db_path: str | Path = DEFAULT_READINESS_DB) -> None:
        self.db_path = Path(db_path)
        init_simulated_order_db(self.db_path)

    def route_order(
        self,
        *,
        exchange: str,
        market: str,
        side: str,
        price: float,
        size: float,
        strategy_id: str | None = None,
        has_open_position: bool = False,
        market_data_stale: bool = False,
    ) -> dict[str, Any]:
        gate = check_execution_gate(
            strategy_id=strategy_id,
            exchange=exchange,
            market=market,
            readiness_db_path=self.db_path,
            has_open_position=has_open_position,
            market_data_stale=market_data_stale,
        )
        status = "simulated" if gate.allowed else "rejected"
        rejection_reason = "; ".join(gate.reasons) if gate.reasons else None
        risk_decision = {"blocked": gate.blocked, "paper_only": True}
        now = _utc_now()
        with closing_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO simulated_orders (
                    exchange, market, side, price, size, strategy_id,
                    risk_decision_json, gate_result_json, status, rejection_reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exchange,
                    market,
                    side,
                    float(price),
                    float(size),
                    strategy_id,
                    json.dumps(risk_decision, sort_keys=True),
                    json.dumps(gate.to_dict(), sort_keys=True),
                    status,
                    rejection_reason,
                    now,
                ),
            )
        return {
            "read_only": True,
            "paper_only": True,
            "accepted": gate.allowed,
            "status": status,
            "rejection_reason": rejection_reason,
            "gate": gate.to_dict(),
            "order": {
                "exchange": exchange,
                "market": market,
                "side": side,
                "price": price,
                "size": size,
                "strategy_id": strategy_id,
                "recorded_at": now,
            },
        }

    def load_recent_orders(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with closing_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT exchange, market, side, price, size, strategy_id, status, rejection_reason, recorded_at
                FROM simulated_orders
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (max(int(limit), 0),),
            ).fetchall()
        return [dict(row) for row in rows]
