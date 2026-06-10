from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.risk.models import RiskConfig, RiskDecision, RiskDecisionType, RiskRequest
from src.sqlite_utils import closing_connection

DEFAULT_RISK_DB_PATH = "data/polylens.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RiskEngine:
    def __init__(self, db_path: str | Path = DEFAULT_RISK_DB_PATH, config: RiskConfig | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or RiskConfig.from_env()
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    market TEXT NOT NULL,
                    opportunity_id TEXT,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    score REAL,
                    edge REAL,
                    proposed_stake REAL NOT NULL DEFAULT 0,
                    current_exposure REAL NOT NULL DEFAULT 0,
                    daily_pnl REAL NOT NULL DEFAULT 0,
                    monthly_pnl REAL NOT NULL DEFAULT 0,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    live_trading INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_risk_events_recent ON risk_events(timestamp, opportunity_id, venue, market);
                CREATE TABLE IF NOT EXISTS risk_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_exposure (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    market TEXT NOT NULL,
                    opportunity_id TEXT,
                    exposure_delta REAL NOT NULL,
                    current_exposure REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_position_exposure_market ON position_exposure(venue, market, timestamp);
                CREATE TABLE IF NOT EXISTS trading_halts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    venue TEXT,
                    reason TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    resumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trading_halts_active ON trading_halts(active, scope, venue);
                """
            )
            self._set_state_defaults(conn)

    def evaluate(self, request: RiskRequest, *, record_event: bool = True) -> RiskDecision:
        cfg = self.config
        venue = (request.venue or "unknown").strip().lower() or "unknown"
        market = (request.market or "unknown").strip() or "unknown"
        stake = max(0.0, _float(request.proposed_stake))
        score = _optional_float(request.score)
        edge = _optional_float(request.edge)
        dry_run = cfg.dry_run if request.dry_run is None else bool(request.dry_run)
        live_trading = cfg.live_trading if request.live_trading is None else bool(request.live_trading)

        with self._connect() as conn:
            daily_pnl = _float(self._get_state(conn, "daily_pnl", "0"))
            monthly_pnl = _float(self._get_state(conn, "monthly_pnl", "0"))
            peak_equity = _float(self._get_state(conn, "peak_equity", "0"))
            current_equity = _float(self._get_state(conn, "current_equity", str(peak_equity)))
            drawdown = max(0.0, peak_equity - current_equity)
            venue_exposure = self.exposure_by_venue(conn=conn).get(venue, 0.0)
            market_exposure = self.exposure_by_market(conn=conn).get(f"{venue}:{market}", 0.0)

            decision_type = RiskDecisionType.APPROVE
            reason = "approved"
            approved = True

            halt_reason = self._active_halt_reason(conn, venue)
            if halt_reason:
                decision_type = RiskDecisionType.HALT
                reason = halt_reason
                approved = False
            elif daily_pnl <= -abs(cfg.max_daily_loss):
                decision_type = RiskDecisionType.HALT
                reason = f"max daily loss exceeded: {daily_pnl} <= -{abs(cfg.max_daily_loss)}"
                approved = False
            elif monthly_pnl <= -abs(cfg.max_monthly_loss):
                decision_type = RiskDecisionType.HALT
                reason = f"max monthly loss exceeded: {monthly_pnl} <= -{abs(cfg.max_monthly_loss)}"
                approved = False
            elif drawdown >= abs(cfg.max_drawdown):
                decision_type = RiskDecisionType.HALT
                reason = f"max drawdown exceeded: {drawdown} >= {abs(cfg.max_drawdown)}"
                approved = False
            elif stake > cfg.max_position_size:
                decision_type = RiskDecisionType.REJECT
                reason = f"max position size exceeded: {stake} > {cfg.max_position_size}"
                approved = False
            elif venue_exposure + stake > cfg.max_exposure_per_venue:
                decision_type = RiskDecisionType.REJECT
                reason = f"max exposure per venue exceeded: {venue_exposure + stake} > {cfg.max_exposure_per_venue}"
                approved = False
            elif market_exposure + stake > cfg.max_exposure_per_market:
                decision_type = RiskDecisionType.REJECT
                reason = f"max exposure per market exceeded: {market_exposure + stake} > {cfg.max_exposure_per_market}"
                approved = False
            elif score is not None and score < cfg.min_opportunity_score:
                decision_type = RiskDecisionType.REJECT
                reason = f"minimum opportunity score not met: {score} < {cfg.min_opportunity_score}"
                approved = False
            elif edge is not None and edge < cfg.min_expected_edge:
                decision_type = RiskDecisionType.REJECT
                reason = f"minimum expected edge not met: {edge} < {cfg.min_expected_edge}"
                approved = False
            elif self._is_duplicate(conn, request, venue, market):
                decision_type = RiskDecisionType.REJECT
                reason = "duplicate opportunity suppressed"
                approved = False
            elif live_trading and not dry_run:
                decision_type = RiskDecisionType.PAPER_ONLY
                reason = "live execution requested but blocked; paper/dry-run only"
                approved = True

            decision = RiskDecision(
                decision=decision_type,
                approved=approved,
                reason=reason,
                venue=venue,
                market=market,
                opportunity_id=request.opportunity_id,
                score=score,
                edge=edge,
                proposed_stake=stake,
                current_exposure=round(market_exposure, 6),
                daily_pnl=daily_pnl,
                monthly_pnl=monthly_pnl,
                dry_run=dry_run,
                live_trading=live_trading,
            )
            if record_event:
                self._record_event(conn, decision, request.metadata)
            return decision

    def record_position(
        self,
        venue: str,
        market: str,
        exposure_delta: float,
        opportunity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> float:
        venue = (venue or "unknown").strip().lower() or "unknown"
        market = (market or "unknown").strip() or "unknown"
        with self._connect() as conn:
            current = self.exposure_by_market(conn=conn).get(f"{venue}:{market}", 0.0) + float(exposure_delta or 0.0)
            conn.execute(
                """
                INSERT INTO position_exposure
                (timestamp, venue, market, opportunity_id, exposure_delta, current_exposure, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), venue, market, opportunity_id, float(exposure_delta or 0.0), current, _json(metadata or {})),
            )
            return round(current, 6)

    def halt(self, reason: str, venue: str | None = None) -> int:
        scope = "venue" if venue else "global"
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO trading_halts (timestamp, scope, venue, reason, active) VALUES (?, ?, ?, ?, 1)",
                (utc_now(), scope, venue.lower() if venue else None, reason or "manual halt"),
            )
            return int(cur.lastrowid)

    def resume(self, venue: str | None = None) -> int:
        scope = "venue" if venue else "global"
        if venue:
            sql = "UPDATE trading_halts SET active = 0, resumed_at = ? WHERE active = 1 AND scope = ? AND venue = ?"
            params: tuple[Any, ...] = (utc_now(), scope, venue.lower())
        else:
            sql = "UPDATE trading_halts SET active = 0, resumed_at = ? WHERE active = 1 AND scope = ?"
            params = (utc_now(), scope)
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            return int(cur.rowcount or 0)

    def set_pnl(
        self,
        *,
        daily_pnl: float | None = None,
        monthly_pnl: float | None = None,
        current_equity: float | None = None,
        peak_equity: float | None = None,
    ) -> None:
        with self._connect() as conn:
            if daily_pnl is not None:
                self._set_state(conn, "daily_pnl", daily_pnl)
            if monthly_pnl is not None:
                self._set_state(conn, "monthly_pnl", monthly_pnl)
            if current_equity is not None:
                self._set_state(conn, "current_equity", current_equity)
            if peak_equity is not None:
                self._set_state(conn, "peak_equity", peak_equity)

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            state = {row["key"]: _loads(row["value"]) for row in conn.execute("SELECT key, value FROM risk_state")}
            active_halts = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, timestamp, scope, venue, reason FROM trading_halts WHERE active = 1 ORDER BY id DESC"
                )
            ]
            latest_rejection = conn.execute(
                "SELECT timestamp, venue, market, decision, reason FROM risk_events "
                "WHERE decision IN ('REJECT','HALT','PAPER_ONLY') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "mode": self.mode_label(),
                "dry_run": self.config.dry_run,
                "live_trading": self.config.live_trading,
                "live_execution_enabled": False,
                "global_halt": any(row["scope"] == "global" for row in active_halts),
                "venue_halts": [row for row in active_halts if row["scope"] == "venue"],
                "active_halts": active_halts,
                "state": state,
                "exposure_by_venue": self.exposure_by_venue(conn=conn),
                "exposure_by_market": self.exposure_by_market(conn=conn),
                "latest_risk_rejection": dict(latest_rejection) if latest_rejection else None,
                "config": self.config.__dict__,
            }

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))
            ]
        for row in rows:
            row["metadata"] = _loads(row.pop("metadata_json", "{}"))
            row["dry_run"] = bool(row.get("dry_run"))
            row["live_trading"] = bool(row.get("live_trading"))
        return rows

    def record_event(
        self,
        event_type: str,
        *,
        venue: str = "unknown",
        market: str = "unknown",
        opportunity_id: str | None = None,
        decision: RiskDecisionType | str = RiskDecisionType.APPROVE,
        reason: str = "",
        score: float | None = None,
        edge: float | None = None,
        proposed_stake: float = 0.0,
        current_exposure: float = 0.0,
        daily_pnl: float | None = None,
        monthly_pnl: float | None = None,
        dry_run: bool | None = None,
        live_trading: bool | None = None,
        severity: str = "info",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        event_metadata = dict(metadata or {})
        event_metadata["event_type"] = event_type
        event_metadata["severity"] = severity
        decision_value = decision.value if isinstance(decision, RiskDecisionType) else str(decision)
        with self._connect() as conn:
            daily = _float(self._get_state(conn, "daily_pnl", "0")) if daily_pnl is None else float(daily_pnl)
            monthly = _float(self._get_state(conn, "monthly_pnl", "0")) if monthly_pnl is None else float(monthly_pnl)
            cur = conn.execute(
                """
                INSERT INTO risk_events
                (timestamp, venue, market, opportunity_id, decision, reason, score, edge, proposed_stake,
                 current_exposure, daily_pnl, monthly_pnl, dry_run, live_trading, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    (venue or "unknown").strip().lower() or "unknown",
                    (market or "unknown").strip() or "unknown",
                    opportunity_id,
                    decision_value,
                    reason or event_type,
                    score,
                    edge,
                    float(proposed_stake or 0.0),
                    float(current_exposure or 0.0),
                    daily,
                    monthly,
                    int(self.config.dry_run if dry_run is None else dry_run),
                    int(self.config.live_trading if live_trading is None else live_trading),
                    _json(event_metadata),
                ),
            )
            return int(cur.lastrowid)

    def recent_smoke_test_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            row
            for row in self.recent_events(limit=max(1, int(limit)) * 5)
            if (row.get("metadata") or {}).get("event_type", "").startswith("smoke_test_")
        ][:limit]

    def exposure_by_venue(self, *, conn: sqlite3.Connection | None = None) -> dict[str, float]:
        close = conn is None
        conn = conn or self._connect()
        try:
            return {
                str(row["venue"]): round(float(row["exposure"] or 0), 6)
                for row in conn.execute("SELECT venue, SUM(exposure_delta) AS exposure FROM position_exposure GROUP BY venue")
            }
        finally:
            if close:
                conn.close()

    def exposure_by_market(self, *, conn: sqlite3.Connection | None = None) -> dict[str, float]:
        close = conn is None
        conn = conn or self._connect()
        try:
            return {
                f"{row['venue']}:{row['market']}": round(float(row["exposure"] or 0), 6)
                for row in conn.execute(
                    "SELECT venue, market, SUM(exposure_delta) AS exposure FROM position_exposure GROUP BY venue, market"
                )
            }
        finally:
            if close:
                conn.close()

    def mode_label(self) -> str:
        if self.config.live_trading and not self.config.dry_run:
            return "LIVE BLOCKED"
        if self.config.dry_run:
            return "DRY RUN"
        return "PAPER"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            yield conn

    def _set_state_defaults(self, conn: sqlite3.Connection) -> None:
        for key, value in {"daily_pnl": 0.0, "monthly_pnl": 0.0, "current_equity": 0.0, "peak_equity": 0.0}.items():
            conn.execute(
                "INSERT OR IGNORE INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, _json(value), utc_now()),
            )

    def _get_state(self, conn: sqlite3.Connection, key: str, default: str) -> str:
        row = conn.execute("SELECT value FROM risk_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"] if row else default)

    def _set_state(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, _json(value), utc_now()),
        )

    def _active_halt_reason(self, conn: sqlite3.Connection, venue: str) -> str | None:
        row = conn.execute(
            "SELECT reason FROM trading_halts WHERE active = 1 AND scope = 'global' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return f"global trading halt: {row['reason']}"
        row = conn.execute(
            "SELECT reason FROM trading_halts WHERE active = 1 AND scope = 'venue' AND venue = ? ORDER BY id DESC LIMIT 1",
            (venue,),
        ).fetchone()
        if row:
            return f"venue trading halt for {venue}: {row['reason']}"
        return None

    def _is_duplicate(self, conn: sqlite3.Connection, request: RiskRequest, venue: str, market: str) -> bool:
        if self.config.duplicate_opportunity_cooldown_seconds <= 0:
            return False
        key = request.opportunity_id or f"{venue}|{market}|{request.score}|{request.edge}"
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, self.config.duplicate_opportunity_cooldown_seconds))
        cutoff_text = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        rows = conn.execute(
            "SELECT opportunity_id, venue, market, score, edge FROM risk_events WHERE timestamp >= ? ORDER BY id DESC LIMIT 200",
            (cutoff_text,),
        )
        for row in rows:
            row_key = row["opportunity_id"] or f"{row['venue']}|{row['market']}|{row['score']}|{row['edge']}"
            if row_key == key:
                return True
        return False

    def _record_event(self, conn: sqlite3.Connection, decision: RiskDecision, metadata: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO risk_events
            (timestamp, venue, market, opportunity_id, decision, reason, score, edge, proposed_stake,
             current_exposure, daily_pnl, monthly_pnl, dry_run, live_trading, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                decision.venue,
                decision.market,
                decision.opportunity_id,
                decision.decision.value,
                decision.reason,
                decision.score,
                decision.edge,
                decision.proposed_stake,
                decision.current_exposure,
                decision.daily_pnl,
                decision.monthly_pnl,
                int(decision.dry_run),
                int(decision.live_trading),
                _json(metadata or {}),
            ),
        )


def _float(value: Any) -> float:
    try:
        return float(_loads(value))
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
