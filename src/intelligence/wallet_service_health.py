from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.analysis.trader_registry import DEFAULT_TRADERS_DB
from src.intelligence.wallet_autonomy_service import (
    CYCLE_NAMES,
    DEFAULT_CYCLE_INTERVALS_SECONDS,
    SERVICE_KEY,
    WalletAutonomyService,
    init_wallet_service_state_db,
    _parse_ts,
)
from src.intelligence.wallet_seed_import import bootstrap_health_report
from src.sqlite_utils import closing_connection


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bootstrap_is_intentionally_dormant(
    *,
    row: dict[str, Any],
    traders_db_path: str,
    service: WalletAutonomyService,
) -> bool:
    if str(row.get("last_status") or "") != "success":
        return False

    try:
        with closing_connection(traders_db_path) as conn:
            result_row = conn.execute(
                "SELECT last_result_json FROM wallet_service_state WHERE cycle_name = 'bootstrap'"
            ).fetchone()
    except Exception:
        return False

    result_json = result_row["last_result_json"] if result_row else "{}"
    try:
        result = json.loads(str(result_json or "{}"))
    except json.JSONDecodeError:
        return False

    if result.get("skipped") is not True or result.get("reason") != "ecosystem not empty":
        return False

    try:
        health = bootstrap_health_report(
            traders_db_path=traders_db_path,
            discovery_db_path=service.discovery_db_path,
        )
    except Exception:
        return False

    return health.get("ecosystem_empty") is False


def wallet_service_health_summary(
    *,
    traders_db_path: str = DEFAULT_TRADERS_DB,
    service: WalletAutonomyService | None = None,
) -> dict[str, Any]:
    init_wallet_service_state_db(traders_db_path)
    svc = service or WalletAutonomyService(traders_db_path=traders_db_path)
    now = _utc_now()
    states = {row["cycle_name"]: row for row in svc.load_cycle_state()}
    service_row = states.get(SERVICE_KEY, {})

    cycle_health: list[dict[str, Any]] = []
    stale_cycles: list[str] = []
    failures: list[dict[str, Any]] = []
    latencies: list[float] = []
    successes = 0
    attempts = 0

    for cycle_name in CYCLE_NAMES:
        row = states.get(cycle_name, {})
        interval = DEFAULT_CYCLE_INTERVALS_SECONDS.get(cycle_name, 3600)
        last_run_at = _parse_ts(row.get("last_run_at"))
        last_status = str(row.get("last_status") or "never")
        duration_ms = float(row.get("last_duration_ms") or 0.0)
        stale = False
        if last_run_at is None:
            stale = True
        else:
            age_seconds = (now - last_run_at).total_seconds()
            stale = age_seconds > interval * 2
        if stale and cycle_name == "bootstrap" and _bootstrap_is_intentionally_dormant(
            row=row,
            traders_db_path=traders_db_path,
            service=svc,
        ):
            stale = False
        if stale:
            stale_cycles.append(cycle_name)
        if last_status == "error":
            failures.append({"cycle": cycle_name, "error": row.get("last_error")})
        if last_status in {"success", "error"}:
            attempts += 1
            if last_status == "success":
                successes += 1
        if duration_ms > 0:
            latencies.append(duration_ms)
        cycle_health.append(
            {
                "cycle": cycle_name,
                "last_run_at": row.get("last_run_at"),
                "last_status": last_status,
                "health_status": row.get("health_status"),
                "duration_ms": duration_ms,
                "stale": stale,
                "interval_seconds": interval,
            }
        )

    with closing_connection(traders_db_path) as conn:
        recent_runs = conn.execute(
            """
            SELECT cycle_name, status, finished_at, duration_ms, error
            FROM wallet_service_cycle_runs
            ORDER BY finished_at DESC, id DESC
            LIMIT 100
            """
        ).fetchall()
    recent_failures = [
        dict(row) for row in recent_runs if str(row["status"]) == "error"
    ][:10]

    success_rate = round(successes / max(1, attempts), 4)
    avg_latency_ms = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
    service_started = service_row.get("last_run_at")
    overall_status = "healthy"
    if failures or stale_cycles:
        overall_status = "degraded"
    if len(stale_cycles) >= len(CYCLE_NAMES):
        overall_status = "unhealthy"

    return {
        "read_only": True,
        "paper_only": True,
        "status": overall_status,
        "success_rate": success_rate,
        "failures": failures,
        "recent_failures": recent_failures,
        "stale_cycles": stale_cycles,
        "avg_latency_ms": avg_latency_ms,
        "service_uptime_anchor": service_started,
        "cycles": cycle_health,
        "checked_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
