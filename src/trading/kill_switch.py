from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sqlite_utils import closing_connection

DEFAULT_READINESS_DB = "data/trading_readiness.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_kill_switch_db(db_path: str | Path = DEFAULT_READINESS_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kill_switch_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                strategy_id TEXT,
                wallet TEXT,
                market TEXT,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                recorded_at TEXT NOT NULL,
                resumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_kill_switch_active ON kill_switch_events(active, scope, strategy_id, market);
            """
        )


class KillSwitch:
    def __init__(self, db_path: str | Path = DEFAULT_READINESS_DB) -> None:
        self.db_path = Path(db_path)
        init_kill_switch_db(self.db_path)

    def halt(
        self,
        *,
        scope: str = "global",
        strategy_id: str | None = None,
        wallet: str | None = None,
        market: str | None = None,
        reason: str = "manual halt",
    ) -> dict[str, Any]:
        now = _utc_now()
        with closing_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO kill_switch_events (
                    scope, strategy_id, wallet, market, action, reason, active, recorded_at
                ) VALUES (?, ?, ?, ?, 'halt', ?, 1, ?)
                """,
                (scope, strategy_id, wallet, market, reason, now),
            )
        return {"scope": scope, "action": "halt", "reason": reason, "active": True, "recorded_at": now}

    def resume(
        self,
        *,
        scope: str = "global",
        strategy_id: str | None = None,
        market: str | None = None,
        reason: str = "manual resume",
    ) -> dict[str, Any]:
        now = _utc_now()
        with closing_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE kill_switch_events
                SET active = 0, resumed_at = ?, reason = ?
                WHERE active = 1 AND scope = ? AND COALESCE(strategy_id, '') = COALESCE(?, '')
                  AND COALESCE(market, '') = COALESCE(?, '')
                """,
                (now, reason, scope, strategy_id, market),
            )
        return {"scope": scope, "action": "resume", "reason": reason, "active": False, "recorded_at": now}

    def is_halted(
        self,
        *,
        scope: str = "global",
        strategy_id: str | None = None,
        wallet: str | None = None,
        market: str | None = None,
    ) -> bool:
        with closing_connection(self.db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM kill_switch_events WHERE active = 1 AND scope = 'global' LIMIT 1"
            ).fetchone():
                return True
            if strategy_id and conn.execute(
                "SELECT 1 FROM kill_switch_events WHERE active = 1 AND scope = 'strategy' AND strategy_id = ? LIMIT 1",
                (strategy_id,),
            ).fetchone():
                return True
            if wallet and conn.execute(
                "SELECT 1 FROM kill_switch_events WHERE active = 1 AND scope = 'wallet' AND wallet = ? LIMIT 1",
                (wallet,),
            ).fetchone():
                return True
            if market and conn.execute(
                "SELECT 1 FROM kill_switch_events WHERE active = 1 AND scope = 'market' AND market = ? LIMIT 1",
                (market,),
            ).fetchone():
                return True
        return False

    def status(self) -> dict[str, Any]:
        with closing_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT scope, strategy_id, wallet, market, reason, recorded_at
                FROM kill_switch_events
                WHERE active = 1
                ORDER BY recorded_at DESC
                """
            ).fetchall()
        return {
            "read_only": True,
            "paper_only": True,
            "halted": bool(rows),
            "active_halts": [dict(row) for row in rows],
        }
