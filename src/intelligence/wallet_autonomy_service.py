from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB
from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.intelligence.wallet_discovery import WalletDiscoveryEngine, init_wallet_discovery_db
from src.intelligence.wallet_feedback_engine import WalletFeedbackEngine, run_wallet_feedback_cycle
from src.intelligence.wallet_performance import WalletPerformanceEngine, init_wallet_performance_db
from src.intelligence.wallet_performance_analytics import wallet_performance_analytics_report
from src.intelligence.wallet_signal_integration import run_wallet_signal_integration_cycle
from src.intelligence.wallet_signal_analytics import wallet_signal_analytics_report
from src.intelligence.wallet_discovery_analytics import wallet_discovery_analytics_report
from src.intelligence.wallet_tracker import WalletTracker
from src.sqlite_utils import closing_connection

CYCLE_NAMES = ("acquisition", "discovery", "signals", "performance", "feedback", "analytics", "alpha")
SERVICE_KEY = "__service__"
DEFAULT_CYCLE_INTERVALS_SECONDS = {
    "acquisition": 6 * 3600,
    "discovery": 6 * 3600,
    "signals": 5 * 60,
    "performance": 3600,
    "feedback": 3600,
    "analytics": 6 * 3600,
    "alpha": 24 * 3600,
}


@dataclass
class WalletAutonomyConfig:
    discovery_limit: int = 50
    watchlist_limit: int = 50
    sync_limit: int = 20
    performance_limit: int = 50
    feedback_limit: int = 50
    analytics_limit: int = 15
    cycle_intervals_seconds: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_CYCLE_INTERVALS_SECONDS)
    )
    run_copy_trader: bool = True


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def init_wallet_service_state_db(
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> None:
    init_wallet_discovery_db(traders_db_path, discovery_db_path)
    init_wallet_performance_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_service_state (
                cycle_name TEXT PRIMARY KEY,
                last_run_at TEXT,
                last_duration_ms REAL,
                last_status TEXT NOT NULL DEFAULT 'never',
                last_error TEXT,
                last_result_json TEXT NOT NULL DEFAULT '{}',
                health_status TEXT NOT NULL DEFAULT 'unknown',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_service_cycle_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS wallet_autonomy_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_json TEXT NOT NULL,
                generated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_service_cycle_runs_name ON wallet_service_cycle_runs(cycle_name, finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_autonomy_reports_time ON wallet_autonomy_reports(generated_at DESC);
            """
        )
        now = _utc_now()
        for cycle_name in (*CYCLE_NAMES, SERVICE_KEY):
            conn.execute(
                """
                INSERT OR IGNORE INTO wallet_service_state (cycle_name, last_status, updated_at)
                VALUES (?, 'never', ?)
                """,
                (cycle_name, now),
            )


class WalletAutonomyService:
    """Orchestrate wallet intelligence cycles using existing modules."""

    def __init__(
        self,
        *,
        config: WalletAutonomyConfig | None = None,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        discovery_engine: WalletDiscoveryEngine | None = None,
        tracker: WalletTracker | None = None,
        performance_engine: WalletPerformanceEngine | None = None,
        feedback_engine: WalletFeedbackEngine | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or WalletAutonomyConfig()
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self._clock = clock
        self._discovery = discovery_engine or WalletDiscoveryEngine(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
        )
        self._tracker = tracker or WalletTracker(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
        )
        self._performance = performance_engine or WalletPerformanceEngine(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
        )
        self._feedback = feedback_engine or WalletFeedbackEngine(
            traders_db_path=str(self.traders_db_path),
            discovery_db_path=str(self.discovery_db_path),
        )
        init_wallet_service_state_db(self.traders_db_path)

    def load_cycle_state(self, cycle_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT cycle_name, last_run_at, last_duration_ms, last_status, last_error, health_status, updated_at FROM wallet_service_state"
        params: list[Any] = []
        if cycle_name:
            query += " WHERE cycle_name = ?"
            params.append(cycle_name)
        query += " ORDER BY cycle_name ASC"
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def load_cycle_runs(self, *, cycle_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT cycle_name, started_at, finished_at, duration_ms, status, error, result_json
            FROM wallet_service_cycle_runs
        """
        params: list[Any] = []
        if cycle_name:
            query += " WHERE cycle_name = ?"
            params.append(cycle_name)
        query += " ORDER BY finished_at DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "result": json.loads(row["result_json"] or "{}"),
            }
            for row in rows
        ]

    def _persist_cycle_run(
        self,
        *,
        cycle_name: str,
        started_at: str,
        finished_at: str,
        duration_ms: float,
        status: str,
        error: str | None,
        result: dict[str, Any],
        health_status: str,
    ) -> None:
        result_json = json.dumps(result, sort_keys=True)
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_service_cycle_runs (
                    cycle_name, started_at, finished_at, duration_ms, status, error, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cycle_name, started_at, finished_at, duration_ms, status, error, result_json),
            )
            conn.execute(
                """
                INSERT INTO wallet_service_state (
                    cycle_name, last_run_at, last_duration_ms, last_status, last_error,
                    last_result_json, health_status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_name) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    last_duration_ms = excluded.last_duration_ms,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    last_result_json = excluded.last_result_json,
                    health_status = excluded.health_status,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle_name,
                    finished_at,
                    duration_ms,
                    status,
                    error,
                    result_json,
                    health_status,
                    finished_at,
                ),
            )

    def _run_wrapped(self, cycle_name: str, runner: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = _utc_now()
        start_ts = self._clock()
        try:
            result = _with_flags(runner())
            duration_ms = round((self._clock() - start_ts) * 1000.0, 2)
            finished = _utc_now()
            self._persist_cycle_run(
                cycle_name=cycle_name,
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                status="success",
                error=None,
                result=result,
                health_status="healthy",
            )
            return {"cycle": cycle_name, "status": "success", "duration_ms": duration_ms, "result": result}
        except Exception as exc:
            duration_ms = round((self._clock() - start_ts) * 1000.0, 2)
            finished = _utc_now()
            error = str(exc)
            result = {"error": error}
            self._persist_cycle_run(
                cycle_name=cycle_name,
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                status="error",
                error=error,
                result=result,
                health_status="unhealthy",
            )
            return {"cycle": cycle_name, "status": "error", "duration_ms": duration_ms, "error": error}

    def run_acquisition_cycle(self) -> dict[str, Any]:
        from src.intelligence.wallet_data_acquisition import run_wallet_acquisition

        return self._run_wrapped(
            "acquisition",
            lambda: run_wallet_acquisition(
                traders_db_path=self.traders_db_path,
                discovery_db_path=self.discovery_db_path,
                limit=self.config.discovery_limit,
            ),
        )

    def run_discovery_cycle(self) -> dict[str, Any]:
        return self._run_wrapped(
            "discovery",
            lambda: self._discovery.run_discovery_cycle(limit=self.config.discovery_limit),
        )

    def run_signals_cycle(self) -> dict[str, Any]:
        return self._run_wrapped(
            "signals",
            lambda: run_wallet_signal_integration_cycle(
                wallet_tracker=self._tracker,
                watchlist_limit=self.config.watchlist_limit,
                sync_limit=self.config.sync_limit,
                run_copy_trader=self.config.run_copy_trader,
            ),
        )

    def run_performance_cycle(self) -> dict[str, Any]:
        def _runner() -> dict[str, Any]:
            wallets = [
                str(row["wallet"])
                for row in self._discovery.load_lifecycle(limit=self.config.performance_limit)
            ]
            if not wallets:
                wallets = [
                    str(row["wallet"])
                    for row in self._discovery.load_recent_discoveries(limit=self.config.performance_limit)
                ]
            scored = []
            for wallet in wallets:
                performance = self._performance.score_wallet(wallet)
                self._performance.persist_snapshot(performance)
                scored.append(performance.to_dict())
            return {"evaluated": len(scored), "wallets": scored[:10]}

        return self._run_wrapped("performance", _runner)

    def run_feedback_cycle(self) -> dict[str, Any]:
        def _runner() -> dict[str, Any]:
            wallets = [
                str(row["wallet"])
                for row in self._discovery.load_lifecycle(limit=self.config.feedback_limit)
            ]
            return run_wallet_feedback_cycle(
                traders_db_path=str(self.traders_db_path),
                discovery_db_path=str(self.discovery_db_path),
                wallets=wallets or None,
            )

        return self._run_wrapped("feedback", _runner)

    def run_analytics_cycle(self) -> dict[str, Any]:
        def _runner() -> dict[str, Any]:
            discovery = wallet_discovery_analytics_report(
                traders_db_path=self.traders_db_path,
                discovery_db_path=self.discovery_db_path,
                top_limit=self.config.analytics_limit,
            )
            performance = wallet_performance_analytics_report(
                traders_db_path=self.traders_db_path,
                discovery_db_path=self.discovery_db_path,
                top_limit=self.config.analytics_limit,
            )
            signals = wallet_signal_analytics_report(
                traders_db_path=self.traders_db_path,
                paper_copy_db_path=self._tracker.paper_copy_db_path,
                wallet_limit=self.config.analytics_limit,
            )
            report = wallet_autonomy_report(
                traders_db_path=self.traders_db_path,
                discovery_db_path=self.discovery_db_path,
                service=self,
            )
            return {
                "discovery_analytics": discovery,
                "performance_analytics": performance,
                "signal_analytics": signals,
                "autonomy_report": report,
            }

        return self._run_wrapped("analytics", _runner)

    def run_alpha_cycle(self) -> dict[str, Any]:
        from src.intelligence.wallet_alpha_lab import run_wallet_alpha_lab_cycle

        return self._run_wrapped(
            "alpha",
            lambda: run_wallet_alpha_lab_cycle(
                traders_db_path=self.traders_db_path,
                discovery_db_path=self.discovery_db_path,
                limit=self.config.analytics_limit,
            ),
        )

    def run_cycle(self, cycle_name: str) -> dict[str, Any]:
        runners = {
            "acquisition": self.run_acquisition_cycle,
            "discovery": self.run_discovery_cycle,
            "signals": self.run_signals_cycle,
            "performance": self.run_performance_cycle,
            "feedback": self.run_feedback_cycle,
            "analytics": self.run_analytics_cycle,
            "alpha": self.run_alpha_cycle,
        }
        if cycle_name not in runners:
            raise ValueError(f"Unknown cycle: {cycle_name}")
        return runners[cycle_name]()

    def cycle_is_due(self, cycle_name: str, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        interval = self.config.cycle_intervals_seconds.get(cycle_name, DEFAULT_CYCLE_INTERVALS_SECONDS[cycle_name])
        rows = self.load_cycle_state(cycle_name)
        if not rows:
            return True
        last_run_at = _parse_ts(rows[0].get("last_run_at"))
        if last_run_at is None:
            return True
        elapsed = (now - last_run_at).total_seconds()
        return elapsed >= interval

    def run_due_cycles(self, *, force: bool = False) -> dict[str, Any]:
        started = _utc_now()
        start_ts = self._clock()
        results: list[dict[str, Any]] = []
        for cycle_name in CYCLE_NAMES:
            if force or self.cycle_is_due(cycle_name):
                results.append(self.run_cycle(cycle_name))
        finished = _utc_now()
        duration_ms = round((self._clock() - start_ts) * 1000.0, 2)
        payload = _with_flags(
            {
                "started_at": started,
                "finished_at": finished,
                "duration_ms": duration_ms,
                "cycles_run": len(results),
                "results": results,
            }
        )
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_service_state (
                    cycle_name, last_run_at, last_duration_ms, last_status, last_error,
                    last_result_json, health_status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_name) DO UPDATE SET
                    last_run_at = excluded.last_run_at,
                    last_duration_ms = excluded.last_duration_ms,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    last_result_json = excluded.last_result_json,
                    health_status = excluded.health_status,
                    updated_at = excluded.updated_at
                """,
                (
                    SERVICE_KEY,
                    finished,
                    duration_ms,
                    "success" if all(row.get("status") == "success" for row in results) else "partial",
                    None,
                    json.dumps(payload, sort_keys=True),
                    "healthy" if results else "idle",
                    finished,
                ),
            )
        return payload

    def run_all_cycles(self) -> dict[str, Any]:
        return self.run_due_cycles(force=True)

    def service_status(self) -> dict[str, Any]:
        states = {row["cycle_name"]: row for row in self.load_cycle_state()}
        service_row = states.get(SERVICE_KEY, {})
        cycle_rows = [states[name] for name in CYCLE_NAMES if name in states]
        return _with_flags(
            {
                "service": service_row,
                "cycles": cycle_rows,
                "intervals_seconds": dict(self.config.cycle_intervals_seconds),
                "due": {name: self.cycle_is_due(name) for name in CYCLE_NAMES},
            }
        )


def wallet_autonomy_report(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    service: WalletAutonomyService | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    init_wallet_service_state_db(traders_db_path)
    svc = service or WalletAutonomyService(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    discovery = WalletDiscoveryEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    performance = WalletPerformanceEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)

    discovered = discovery.load_recent_discoveries(limit=25)
    promoted = performance.load_actions(action="promoted", limit=25)
    retired = performance.load_actions(action="retired", limit=25)
    score_drift = []
    with closing_connection(traders_db_path) as conn:
        rows = conn.execute(
            """
            SELECT wallet, score_delta, previous_score, current_score, trend, recorded_at
            FROM wallet_performance_trends
            ORDER BY recorded_at DESC, id DESC
            LIMIT 25
            """
        ).fetchall()
        score_drift = [dict(row) for row in rows]

    performance_summary = wallet_performance_analytics_report(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        top_limit=15,
    )

    report = _with_flags(
        {
            "generated_at": _utc_now(),
            "discovered_wallets": discovered,
            "promoted_wallets": promoted,
            "retired_wallets": retired,
            "score_changes": score_drift,
            "performance_summary": {
                "wallet_count": performance_summary.get("wallet_count"),
                "status_distribution": performance_summary.get("status_distribution"),
                "top_performers": performance_summary.get("top_performers", [])[:10],
            },
            "service_status": svc.service_status(),
        }
    )

    if persist:
        with closing_connection(traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_autonomy_reports (report_json, generated_at)
                VALUES (?, ?)
                """,
                (json.dumps(report, sort_keys=True), report["generated_at"]),
            )
    return report


def load_wallet_autonomy_reports(*, traders_db_path: str | Path = DEFAULT_TRADERS_DB, limit: int = 10) -> list[dict[str, Any]]:
    init_wallet_service_state_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        rows = conn.execute(
            """
            SELECT report_json, generated_at
            FROM wallet_autonomy_reports
            ORDER BY generated_at DESC, id DESC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    return [
        {**json.loads(row["report_json"]), "generated_at": row["generated_at"]}
        for row in rows
    ]


def run_wallet_autonomy_service(*, force: bool = False, traders_db_path: str | Path = DEFAULT_TRADERS_DB) -> dict[str, Any]:
    service = WalletAutonomyService(traders_db_path=traders_db_path)
    return service.run_due_cycles(force=force)
