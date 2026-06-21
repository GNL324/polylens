#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path("/home/noel/polylens")
CANONICAL_TRADERS_DB = REPO_ROOT / "data" / "traders.db"
ROOT_TRADERS_DB = REPO_ROOT / "traders.db"
CANONICAL_SIGNAL_DB = REPO_ROOT / "data" / "trader_signals.db"
CANONICAL_ACTIVITY_DB = REPO_ROOT / "data" / "wallet_activity.db"
DB_ERROR_PATTERNS = ("UNIQUE constraint", "database is locked", "database disk image", "sqlite", "constraint failed")


@dataclass
class Finding:
    level: str
    code: str
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": self.details or {},
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def root_db_hygiene(root_db: Path = ROOT_TRADERS_DB, *, remove_zero_byte: bool = True) -> list[Finding]:
    findings: list[Finding] = []
    if not root_db.exists():
        findings.append(Finding("info", "root_traders_db_absent", "Root traders.db artifact is absent."))
        return findings
    size = root_db.stat().st_size
    if size == 0:
        if remove_zero_byte:
            root_db.unlink()
            findings.append(Finding("info", "root_traders_db_removed", "Removed zero-byte root traders.db hygiene artifact."))
        else:
            findings.append(Finding("info", "root_traders_db_ignored", "Ignoring zero-byte root traders.db hygiene artifact."))
        return findings
    findings.append(
        Finding(
            "warning",
            "root_traders_db_ignored_nonzero",
            "Ignoring non-canonical root traders.db; production checks use data/traders.db only.",
            {"path": str(root_db), "bytes": size},
        )
    )
    return findings


def check_sqlite_db(path: Path, *, code_prefix: str) -> list[Finding]:
    if not path.exists():
        return [Finding("alert", f"{code_prefix}_missing", f"Canonical database missing: {path}")]
    if path.stat().st_size == 0:
        return [Finding("alert", f"{code_prefix}_empty", f"Canonical database is zero bytes: {path}")]
    uri = f"file:{path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    except sqlite3.Error as exc:
        return [Finding("alert", f"{code_prefix}_sqlite_error", f"Canonical database failed read-only check: {path}", {"error": str(exc)})]
    if integrity != "ok":
        return [Finding("alert", f"{code_prefix}_integrity", f"Canonical database integrity_check failed: {path}", {"integrity": integrity})]
    return [Finding("info", f"{code_prefix}_ok", f"Canonical database passed read-only integrity check.", {"path": str(path), "tables": int(tables)})]


def http_status(url: str, *, timeout: float = 5.0) -> int | None:
    try:
        request = Request(url, method="GET", headers={"User-Agent": "polylens-sre-check/1.0"})
        opener = urlopen(request, timeout=timeout)
        with opener as response:
            return int(response.status)
    except Exception as exc:
        # urllib follows redirects by default, so ask curl-compatible callers to use this only for terminal health.
        if hasattr(exc, "code"):
            return int(exc.code)  # type: ignore[attr-defined]
        return None


def raw_http_status(url: str, *, timeout: float = 5.0) -> int | None:
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return http_status(url, timeout=timeout)
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def tcp_listeners() -> list[str]:
    result = subprocess.run(["ss", "-tlnH"], text=True, capture_output=True, check=False)
    return [line.split()[3] for line in result.stdout.splitlines() if line.split()]


def listener_health(listeners: list[str], *, ports: tuple[int, ...] = (8787, 8788)) -> list[Finding]:
    findings: list[Finding] = []
    for port in ports:
        suffix = f":{port}"
        matching = [addr for addr in listeners if addr.endswith(suffix)]
        exposed = [addr for addr in matching if addr.startswith("0.0.0.0:") or addr.startswith("[::]:") or addr.startswith(":::")]
        localhost = [addr for addr in matching if addr.startswith("127.0.0.1:") or addr.startswith("[::1]:")]
        if exposed:
            findings.append(Finding("alert", f"dashboard_exposed_{port}", f"Dashboard port {port} is exposed beyond localhost.", {"listeners": exposed}))
        elif localhost:
            findings.append(Finding("info", f"dashboard_localhost_{port}", f"Dashboard port {port} is bound to localhost.", {"listeners": localhost}))
        else:
            findings.append(Finding("alert", f"dashboard_missing_{port}", f"Dashboard port {port} has no localhost listener.", {"listeners": matching}))
    return findings


def dashboard_http_health() -> list[Finding]:
    findings: list[Finding] = []
    root_status = raw_http_status("http://127.0.0.1:8787/")
    mission_status = raw_http_status("http://127.0.0.1:8787/mission-control")
    trader_status = raw_http_status("http://127.0.0.1:8788/")
    if root_status == 307:
        findings.append(Finding("info", "dashboard_root_redirect_ok", "Main dashboard root returns expected HTTP 307 redirect."))
    else:
        findings.append(Finding("alert", "dashboard_root_redirect_bad", "Main dashboard root did not return expected HTTP 307.", {"status": root_status}))
    if mission_status == 200:
        findings.append(Finding("info", "mission_control_ok", "/mission-control returns HTTP 200."))
    else:
        findings.append(Finding("alert", "mission_control_bad", "/mission-control health endpoint failed.", {"status": mission_status}))
    if trader_status == 200:
        findings.append(Finding("info", "trader_dashboard_ok", "Trader dashboard returns HTTP 200 on 8788."))
    else:
        findings.append(Finding("alert", "trader_dashboard_bad", "Trader dashboard failed on 8788.", {"status": trader_status}))
    return findings


def recent_failure_findings(db_path: Path, *, now: datetime | None = None, recent_window_minutes: int = 30) -> list[Finding]:
    now = now or utc_now()
    window_start = now - timedelta(minutes=recent_window_minutes)
    findings: list[Finding] = []
    if not db_path.exists():
        return [Finding("alert", "wallet_service_db_missing", "Cannot inspect wallet service failures; canonical traders DB is missing.")]
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        runs = [dict(row) for row in conn.execute(
            """
            SELECT cycle_name, status, finished_at, error
            FROM wallet_service_cycle_runs
            ORDER BY finished_at DESC, rowid DESC
            LIMIT 500
            """
        )]
    latest_success: dict[str, datetime] = {}
    for row in runs:
        if row.get("status") == "success":
            finished = parse_ts(row.get("finished_at"))
            if finished and row["cycle_name"] not in latest_success:
                latest_success[row["cycle_name"]] = finished
    alerts = 0
    resolved = 0
    for row in runs:
        if row.get("status") != "error" or not is_db_signal_error(row.get("error")):
            continue
        finished = parse_ts(row.get("finished_at"))
        cycle = str(row.get("cycle_name") or "")
        cleared_at = latest_success.get(cycle)
        if finished and cleared_at and cleared_at > finished:
            resolved += 1
            continue
        if (finished and finished >= window_start) or not cleared_at:
            alerts += 1
            findings.append(
                Finding(
                    "alert",
                    "recent_uncleared_wallet_service_error",
                    "Recent wallet service DB/signals error has not been cleared by later success.",
                    {"cycle": cycle, "finished_at": row.get("finished_at"), "error": row.get("error")},
                )
            )
    if resolved:
        findings.append(Finding("info", "historical_errors_resolved", "Historical wallet service DB/signals errors were cleared by later successful cycles.", {"count": resolved}))
    if alerts == 0:
        findings.append(Finding("info", "no_recent_uncleared_errors", "No recent uncleared wallet service DB/signals errors."))
    return findings


def is_db_signal_error(error: Any) -> bool:
    text = str(error or "")
    return any(pattern.lower() in text.lower() for pattern in DB_ERROR_PATTERNS)


def wallet_service_health() -> list[Finding]:
    try:
        result = subprocess.run(
            [str(REPO_ROOT / ".venv" / "bin" / "python"), "-m", "src.cli", "wallet-service-health", "--json"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [Finding("alert", "wallet_service_health_failed", "wallet-service-health command failed.", {"error": str(exc)})]
    if result.returncode != 0:
        return [Finding("alert", "wallet_service_health_nonzero", "wallet-service-health exited non-zero.", {"stderr": result.stderr[-1000:]})]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [Finding("alert", "wallet_service_health_bad_json", "wallet-service-health did not emit JSON.", {"error": str(exc)})]
    if payload.get("status") == "healthy" and not payload.get("stale_cycles") and payload.get("read_only") is True and payload.get("paper_only") is True:
        return [Finding("info", "wallet_service_health_ok", "wallet-service-health is authoritative healthy.", {"status": payload.get("status"), "success_rate": payload.get("success_rate")})]
    return [Finding("alert", "wallet_service_health_unhealthy", "wallet-service-health reported unhealthy/degraded state.", payload)]


def systemd_service_health(*, timeout_minutes: int = 10) -> list[Finding]:
    result = subprocess.run(["systemctl", "show", "wallet-autonomy.service", "--no-page"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return [Finding("warning", "wallet_autonomy_systemd_unavailable", "Could not inspect wallet-autonomy.service.", {"stderr": result.stderr[-1000:]})]
    props = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    active = props.get("ActiveState")
    sub = props.get("SubState")
    result_code = props.get("Result")
    exec_status = props.get("ExecMainStatus")
    if active == "inactive" and result_code == "success" and exec_status in {"0", ""}:
        return [Finding("info", "wallet_autonomy_oneshot_success", "wallet-autonomy.service oneshot inactive success is healthy.", {"ActiveState": active, "SubState": sub, "Result": result_code})]
    if active == "activating":
        entered = parse_systemd_usec(props.get("ActiveEnterTimestampMonotonic"))
        uptime = system_uptime_seconds()
        activating_seconds = None if entered is None or uptime is None else max(0.0, uptime - entered)
        if activating_seconds is not None and activating_seconds <= timeout_minutes * 60:
            return [Finding("info", "wallet_autonomy_activating_recent", "wallet-autonomy.service is activating within timeout.", {"seconds": activating_seconds})]
        return [Finding("alert", "wallet_autonomy_activating_stale", "wallet-autonomy.service activating exceeded timeout.", {"seconds": activating_seconds})]
    if active == "active":
        return [Finding("info", "wallet_autonomy_active", "wallet-autonomy.service is active.", {"SubState": sub})]
    return [Finding("alert", "wallet_autonomy_service_bad_state", "wallet-autonomy.service is not in a healthy oneshot state.", {"ActiveState": active, "SubState": sub, "Result": result_code, "ExecMainStatus": exec_status})]


def parse_systemd_usec(value: Any) -> float | None:
    try:
        raw = int(str(value or "0"))
    except ValueError:
        return None
    return raw / 1_000_000.0 if raw > 0 else None


def system_uptime_seconds() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return None


def run_check(*, recent_window_minutes: int = 30, remove_root_artifact: bool = True) -> dict[str, Any]:
    findings: list[Finding] = []
    findings.extend(root_db_hygiene(remove_zero_byte=remove_root_artifact))
    findings.extend(check_sqlite_db(CANONICAL_TRADERS_DB, code_prefix="canonical_traders_db"))
    findings.extend(check_sqlite_db(CANONICAL_SIGNAL_DB, code_prefix="canonical_trader_signals_db"))
    findings.extend(check_sqlite_db(CANONICAL_ACTIVITY_DB, code_prefix="canonical_wallet_activity_db"))
    findings.extend(listener_health(tcp_listeners()))
    findings.extend(dashboard_http_health())
    findings.extend(wallet_service_health())
    findings.extend(systemd_service_health())
    findings.extend(recent_failure_findings(CANONICAL_TRADERS_DB, recent_window_minutes=recent_window_minutes))
    alert_count = sum(1 for finding in findings if finding.level == "alert")
    warning_count = sum(1 for finding in findings if finding.level == "warning")
    return {
        "status": "healthy" if alert_count == 0 else "alert",
        "alert_count": alert_count,
        "warning_count": warning_count,
        "canonical_paths": {
            "traders_db": str(CANONICAL_TRADERS_DB),
            "trader_signals_db": str(CANONICAL_SIGNAL_DB),
            "wallet_activity_db": str(CANONICAL_ACTIVITY_DB),
        },
        "findings": [finding.to_dict() for finding in findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polylens wallet autonomy SRE health check.")
    parser.add_argument("--recent-window-minutes", type=int, default=int(os.getenv("POLYLENS_SRE_RECENT_WINDOW_MINUTES", "30")))
    parser.add_argument("--no-remove-root-artifact", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_check(recent_window_minutes=args.recent_window_minutes, remove_root_artifact=not args.no_remove_root_artifact)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"status={report['status']} alerts={report['alert_count']} warnings={report['warning_count']}")
        for finding in report["findings"]:
            print(f"{finding['level'].upper()} {finding['code']}: {finding['message']}")
    return 0 if report["alert_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
