from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection

DEFAULT_READINESS_DB = "data/trading_readiness.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_strategy_approval_db(db_path: str | Path = DEFAULT_READINESS_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_approvals (
                strategy_id TEXT PRIMARY KEY,
                approval_status TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT,
                max_capital REAL,
                max_position_size REAL,
                allowed_markets_json TEXT NOT NULL DEFAULT '[]',
                expiration TEXT,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_approvals_status ON strategy_approvals(approval_status);
            """
        )


class StrategyApprovalRegistry:
    def __init__(self, db_path: str | Path = DEFAULT_READINESS_DB) -> None:
        self.db_path = Path(db_path)
        init_strategy_approval_db(self.db_path)

    def approve_strategy(
        self,
        strategy_id: str,
        *,
        approved_by: str,
        max_capital: float = 0.0,
        max_position_size: float = 0.0,
        allowed_markets: list[str] | None = None,
        expiration: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        with closing_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO strategy_approvals (
                    strategy_id, approval_status, approved_at, approved_by,
                    max_capital, max_position_size, allowed_markets_json, expiration, notes, updated_at
                ) VALUES (?, 'approved', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    approval_status = 'approved',
                    approved_at = excluded.approved_at,
                    approved_by = excluded.approved_by,
                    max_capital = excluded.max_capital,
                    max_position_size = excluded.max_position_size,
                    allowed_markets_json = excluded.allowed_markets_json,
                    expiration = excluded.expiration,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    strategy_id,
                    now,
                    approved_by,
                    max_capital,
                    max_position_size,
                    json.dumps(allowed_markets or [], sort_keys=True),
                    expiration,
                    notes,
                    now,
                ),
            )
        return self.get_approval(strategy_id) or {}

    def revoke_strategy(self, strategy_id: str, *, notes: str = "") -> dict[str, Any]:
        now = _utc_now()
        with closing_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE strategy_approvals
                SET approval_status = 'revoked', notes = ?, updated_at = ?
                WHERE strategy_id = ?
                """,
                (notes, now, strategy_id),
            )
        return self.get_approval(strategy_id) or {"strategy_id": strategy_id, "approval_status": "revoked"}

    def get_approval(self, strategy_id: str | None) -> dict[str, Any] | None:
        if not strategy_id:
            return None
        with closing_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM strategy_approvals WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["allowed_markets"] = json.loads(payload.pop("allowed_markets_json") or "[]")
        return payload

    def list_approvals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM strategy_approvals"
        params: list[Any] = []
        if status:
            query += " WHERE approval_status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        with closing_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["allowed_markets"] = json.loads(payload.pop("allowed_markets_json") or "[]")
            results.append(payload)
        return results
