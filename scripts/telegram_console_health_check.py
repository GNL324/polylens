#!/usr/bin/env python3
"""Telegram Console SRE Health Check for Polylens.

Read-only health check that verifies the Telegram console service is
healthy, single-message navigation continues functioning, and live
trading remains disabled.  Produces JSON output compatible with existing
Polylens SRE tools.

Exit codes:
    0 = healthy (no warnings, no errors)
    1 = warnings (non-critical issues found)
    2 = critical (service down, polling stopped, live trading enabled, etc.)

This script is strictly read-only and monitoring-only.  It never modifies
files, databases, or service state.  It sends a Telegram alert *only* when
critical conditions are met.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE_NAME = "polylens-telegram-console.service"
HEALTH_SERVICE_NAME = "polylens-telegram-console-health.service"
REPO_ROOT = Path("/home/noel/polylens")
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
LOG_LINES = 500
RESTART_LOOP_THRESHOLD = 5  # restarts in the log window
REPEATED_ERROR_THRESHOLD = 3  # consecutive identical errors to be "repeated"

# Environment variables that must all be disabled (false) for safety.
LIVE_TRADING_ENV_VARS = [
    "POLYLENS_LIVE_TRADING",
    "LIVE_TRADING",
    "POLYLENS_AUTONOMOUS_CRYPTO",
    "POLYLENS_KALSHI_LIVE_SENDS_ENABLED",
    "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED",
    "POLYLENS_TELEGRAM_LIVE_ENABLED",
]

# Patterns that indicate problems in journal logs.
ERROR_PATTERNS = [
    (re.compile(r"Traceback", re.IGNORECASE), "traceback"),
    (re.compile(r"HTTP 401|HTTPError.*401", re.IGNORECASE), "http_401"),
    (re.compile(r"HTTP 403|HTTPError.*403", re.IGNORECASE), "http_403"),
    (re.compile(r"HTTP 429|HTTPError.*429", re.IGNORECASE), "http_429"),
    (re.compile(r"polling.*(fail|error|stop)", re.IGNORECASE), "polling_failure"),
    (re.compile(r"editMessageText.*fail|edit.*fail", re.IGNORECASE), "edit_failure"),
    (re.compile(r"callback.*(fail|error)|answerCallbackQuery.*fail", re.IGNORECASE), "callback_failure"),
]

# Bot token pattern for leak detection — matches Telegram bot tokens
# (digits:colon-alphanumeric, 35+ chars).
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


@dataclass
class Check:
    name: str
    status: str  # "pass", "warn", "fail"
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class HealthReport:
    status: str  # "healthy", "warning", "critical"
    checks: list[Check]
    warnings: list[str]
    errors: list[str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [c.to_dict() for c in self.checks],
            "warnings": self.warnings,
            "errors": self.errors,
            "metrics": self.metrics,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _systemctl_show(unit: str) -> dict[str, str]:
    """Run systemctl show and parse key=value pairs."""
    result = subprocess.run(
        ["systemctl", "show", unit, "--no-page"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def _systemctl_is_active(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    return (result.stdout or result.stderr or "").strip().lower() or "unknown"


def _journal_lines(unit: str, lines: int = LOG_LINES) -> list[str]:
    """Fetch recent journal lines for a unit."""
    result = subprocess.run(
        ["journalctl", "-u", unit, "--no-pager", "-n", str(lines)],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    return result.stdout.splitlines()


def _parse_systemd_timestamp(raw: str) -> datetime | None:
    """Parse systemd timestamp like 'Fri 2026-06-26 04:57:15 UTC'."""
    if not raw:
        return None
    # Strip weekday prefix if present
    raw = raw.strip()
    for fmt in (
        "%a %Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_uptime_seconds(raw: str) -> float | None:
    """Parse systemd ActiveEnterTimestampMonotonic (microseconds) into seconds since boot."""
    try:
        usec = int(raw)
    except (ValueError, TypeError):
        return None
    if usec <= 0:
        return None
    return usec / 1_000_000.0


def _system_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return None


def _get_env_from_pid(pid: int) -> dict[str, str]:
    """Read environment variables from /proc/<pid>/environ (read-only)."""
    env: dict[str, str] = {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        for entry in raw.split(b"\0"):
            if b"=" in entry:
                key, _, value = entry.partition(b"=")
                env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    except (PermissionError, FileNotFoundError, OSError):
        pass
    return env


def _env_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def check_service_active() -> Check:
    """Verify the Telegram console service is active."""
    state = _systemctl_is_active(SERVICE_NAME)
    if state == "active":
        return Check("service_active", "pass", f"{SERVICE_NAME} is active.")
    return Check(
        "service_active",
        "fail",
        f"{SERVICE_NAME} is not active (state={state}).",
        {"state": state},
    )


def check_process_running() -> Check:
    """Verify the main process is running."""
    props = _systemctl_show(SERVICE_NAME)
    main_pid = props.get("MainPID", "0")
    try:
        pid = int(main_pid)
    except ValueError:
        pid = 0
    if pid > 0:
        proc_path = Path(f"/proc/{pid}")
        if proc_path.exists():
            return Check("process_running", "pass", f"Main PID {pid} is running.", {"pid": pid})
    return Check("process_running", "fail", "Main process is not running.", {"pid": main_pid})


def check_no_restart_loop() -> Check:
    """Detect restart loops by counting restarts in recent logs."""
    lines = _journal_lines(SERVICE_NAME, lines=LOG_LINES)
    restart_count = 0
    for line in lines:
        if "Started" in line and SERVICE_NAME in line:
            restart_count += 1
    if restart_count > RESTART_LOOP_THRESHOLD:
        return Check(
            "no_restart_loop",
            "fail",
            f"Restart loop detected: {restart_count} starts in last {LOG_LINES} log lines.",
            {"restart_count": restart_count, "threshold": RESTART_LOOP_THRESHOLD},
        )
    # A few restarts (e.g. from a deploy) are acceptable
    if restart_count > 1:
        return Check(
            "no_restart_loop",
            "warn",
            f"Service restarted {restart_count} times in recent logs (below loop threshold).",
            {"restart_count": restart_count},
        )
    return Check(
        "no_restart_loop",
        "pass",
        "No restart loop detected.",
        {"restart_count": restart_count},
    )


def check_logs_clean() -> tuple[Check, dict[str, int]]:
    """Scan recent logs for error patterns and token leaks."""
    lines = _journal_lines(SERVICE_NAME, lines=LOG_LINES)
    pattern_counts: dict[str, int] = {}
    token_leaked = False

    for line in lines:
        for regex, label in ERROR_PATTERNS:
            if regex.search(line):
                pattern_counts[label] = pattern_counts.get(label, 0) + 1
        if TOKEN_RE.search(line):
            # Double-check: not just the word "redacted"
            if "redacted" not in line.lower():
                token_leaked = True

    # Determine status
    critical_patterns = {"traceback", "http_401", "http_403"}
    warning_patterns = {"http_429", "polling_failure", "edit_failure", "callback_failure"}

    has_critical = any(pattern_counts.get(p, 0) > 0 for p in critical_patterns)
    has_repeated = any(pattern_counts.get(p, 0) >= REPEATED_ERROR_THRESHOLD for p in warning_patterns)

    details = {
        "pattern_counts": pattern_counts,
        "log_lines_scanned": len(lines),
        "token_leaked": token_leaked,
    }

    if token_leaked:
        return Check("logs_clean", "fail", "Bot token detected in logs — potential security issue.", details), pattern_counts
    if has_critical:
        critical_found = [p for p in critical_patterns if pattern_counts.get(p, 0) > 0]
        return Check("logs_clean", "fail", f"Critical log errors found: {critical_found}", details), pattern_counts
    if has_repeated:
        repeated = [p for p in warning_patterns if pattern_counts.get(p, 0) >= REPEATED_ERROR_THRESHOLD]
        return Check("logs_clean", "warn", f"Repeated warnings in logs: {repeated}", details), pattern_counts
    if pattern_counts:
        return Check("logs_clean", "warn", f"Minor log issues found: {pattern_counts}", details), pattern_counts
    return Check("logs_clean", "pass", "Logs are clean — no errors, exceptions, or token leaks.", details), pattern_counts


def check_paper_only() -> Check:
    """Verify paper_only=true in the service environment."""
    props = _systemctl_show(SERVICE_NAME)
    main_pid = props.get("MainPID", "0")
    try:
        pid = int(main_pid)
    except ValueError:
        pid = 0

    # Try /proc environ first
    env = _get_env_from_pid(pid) if pid > 0 else {}
    paper_only = env.get("POLYLENS_TELEGRAM_PAPER_ONLY", "")

    # Fall back to systemd unit Environment lines
    if not paper_only:
        env_lines = props.get("Environment", "")
        for line in env_lines.split():
            if line.startswith("POLYLENS_TELEGRAM_PAPER_ONLY="):
                paper_only = line.split("=", 1)[1]

    if _env_bool(paper_only):
        return Check("paper_only", "pass", "paper_only=true confirmed.", {"paper_only": True})
    return Check("paper_only", "fail", "paper_only is NOT true — live trading may be enabled!", {"paper_only": paper_only})


def check_live_disabled() -> tuple[Check, dict[str, bool]]:
    """Verify all live trading environment variables are disabled."""
    props = _systemctl_show(SERVICE_NAME)
    main_pid = props.get("MainPID", "0")
    try:
        pid = int(main_pid)
    except ValueError:
        pid = 0

    env = _get_env_from_pid(pid) if pid > 0 else {}
    # Also parse systemd Environment lines
    env_lines = props.get("Environment", "")
    for line in env_lines.split():
        if "=" in line:
            key, _, value = line.partition("=")
            if key not in env:
                env[key] = value

    states: dict[str, bool] = {}
    all_disabled = True
    for var in LIVE_TRADING_ENV_VARS:
        value = env.get(var, "")
        enabled = _env_bool(value)
        states[var] = enabled
        if enabled:
            all_disabled = False

    if all_disabled:
        return Check("live_disabled", "pass", "All live trading environment variables are disabled.", states), states
    enabled_vars = [v for v, on in states.items() if on]
    return Check("live_disabled", "fail", f"Live trading env vars enabled: {enabled_vars}", states), states


def check_polling_active() -> Check:
    """Verify the bot is actively polling (getUpdates in recent logs or service running long enough)."""
    lines = _journal_lines(SERVICE_NAME, lines=LOG_LINES)
    # The service logs "starting telegram console" on startup
    has_startup = any("starting telegram console" in line.lower() for line in lines)
    # If the service is running and has started, polling is active (long-poll getUpdates blocks)
    if has_startup:
        # Check for any polling failure that would stop it
        polling_errors = [line for line in lines if re.search(r"polling.*(fail|error|stop)", line, re.IGNORECASE)]
        if not polling_errors:
            return Check("polling_active", "pass", "Bot polling is functioning (startup logged, no polling errors).", {})
        return Check("polling_active", "warn", f"Polling errors found in logs ({len(polling_errors)}).", {"count": len(polling_errors)})
    return Check("polling_active", "fail", "No startup log found — service may not be polling.", {})


def compute_metrics(props: dict[str, str], pattern_counts: dict[str, int], live_states: dict[str, bool]) -> dict[str, Any]:
    """Compute service metrics from systemd properties and log analysis."""
    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "unknown")

    # Uptime
    enter_ts = _parse_systemd_timestamp(props.get("ActiveEnterTimestamp", ""))
    uptime_seconds: float | None = None
    if enter_ts:
        delta = utc_now() - enter_ts
        uptime_seconds = max(0.0, delta.total_seconds())

    # Memory (from /proc if available)
    memory_mb: float | None = None
    main_pid = props.get("MainPID", "0")
    try:
        pid = int(main_pid)
        if pid > 0:
            status_path = Path(f"/proc/{pid}/status")
            if status_path.exists():
                for line in status_path.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            memory_mb = int(parts[1]) / 1024.0
                        break
    except (ValueError, OSError):
        pass

    # Restart count
    lines = _journal_lines(SERVICE_NAME, lines=LOG_LINES)
    restart_count = sum(1 for line in lines if "Started" in line and SERVICE_NAME in line)

    # NRestarts from systemd
    n_restarts = int(props.get("NRestarts", "0"))

    return {
        "service_name": SERVICE_NAME,
        "active_state": active_state,
        "sub_state": sub_state,
        "uptime_seconds": round(uptime_seconds, 1) if uptime_seconds is not None else None,
        "uptime_human": _human_duration(uptime_seconds) if uptime_seconds is not None else None,
        "memory_mb": round(memory_mb, 2) if memory_mb is not None else None,
        "main_pid": int(main_pid) if main_pid.isdigit() else 0,
        "restart_count": restart_count,
        "n_restarts": n_restarts,
        "callback_errors": pattern_counts.get("callback_failure", 0),
        "edit_failures": pattern_counts.get("edit_failure", 0),
        "polling_failures": pattern_counts.get("polling_failure", 0),
        "http_429_count": pattern_counts.get("http_429", 0),
        "http_401_count": pattern_counts.get("http_401", 0),
        "http_403_count": pattern_counts.get("http_403", 0),
        "traceback_count": pattern_counts.get("traceback", 0),
        "warning_count": sum(1 for v in pattern_counts.values() if 0 < v < REPEATED_ERROR_THRESHOLD),
        "error_count": sum(1 for v in pattern_counts.values() if v >= REPEATED_ERROR_THRESHOLD) + sum(
            1 for p in ("traceback", "http_401", "http_403") if pattern_counts.get(p, 0) > 0
        ),
        "token_leaked": pattern_counts.get("token_leaked", 0) > 0,
        "paper_only": not any(live_states.values()),
        "live_trading_enabled": any(live_states.values()),
    }


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _should_alert(checks: list[Check]) -> bool:
    """Determine if a Telegram alert should be sent (only for critical conditions)."""
    critical_names = {
        "service_active",
        "process_running",
        "polling_active",
        "live_disabled",
        "no_restart_loop",
        "logs_clean",
    }
    for check in checks:
        if check.status == "fail" and check.name in critical_names:
            # For logs_clean, only alert on repeated failures or token leak
            if check.name == "logs_clean":
                # Don't alert on isolated transient errors (single http_429, single edit_failure)
                counts = check.details.get("pattern_counts", {})
                repeated = any(v >= REPEATED_ERROR_THRESHOLD for v in counts.values())
                token_leaked = check.details.get("token_leaked", False)
                has_critical = any(counts.get(p, 0) > 0 for p in ("traceback", "http_401", "http_403"))
                if not (repeated or token_leaked or has_critical):
                    continue
            return True
    return False


def send_telegram_alert(report: HealthReport) -> None:
    """Send a Telegram alert for critical conditions. Best-effort, never raises."""
    try:
        from src.integrations.telegram_console import (
            TelegramConsole,
            TelegramConsoleConfig,
            TelegramResponse,
        )

        config = TelegramConsoleConfig.from_env()
        if not config.bot_token or not config.admin_user_ids:
            return
        console = TelegramConsole(config)
        errors_text = "\n".join(f"• {e}" for e in report.errors[:5])
        message = f"🔴 Telegram Console Health Alert\n\n{errors_text}\n\nStatus: {report.status}"
        response = TelegramResponse(text=message)
        console._send_message(next(iter(config.admin_user_ids)), response)
    except Exception:
        pass  # Never let alerting crash the health check


def run_check(*, suppress_alert: bool = False) -> HealthReport:
    """Run all health checks and produce a HealthReport."""
    checks: list[Check] = []

    # Service and process
    checks.append(check_service_active())
    checks.append(check_process_running())
    checks.append(check_no_restart_loop())
    checks.append(check_polling_active())

    # Logs
    log_check, pattern_counts = check_logs_clean()
    checks.append(log_check)

    # Safety: live trading disabled
    live_check, live_states = check_live_disabled()
    checks.append(live_check)
    checks.append(check_paper_only())

    # Collect warnings and errors
    warnings = [c.message for c in checks if c.status == "warn"]
    errors = [c.message for c in checks if c.status == "fail"]

    # Compute metrics
    props = _systemctl_show(SERVICE_NAME)
    metrics = compute_metrics(props, pattern_counts, live_states)

    # Determine overall status
    fail_count = sum(1 for c in checks if c.status == "fail")
    warn_count = sum(1 for c in checks if c.status == "warn")

    if fail_count > 0:
        status = "critical"
    elif warn_count > 0:
        status = "warning"
    else:
        status = "healthy"

    report = HealthReport(
        status=status,
        checks=checks,
        warnings=warnings,
        errors=errors,
        metrics=metrics,
    )

    # Send Telegram alert only for critical conditions
    if _should_alert(checks) and not suppress_alert:
        send_telegram_alert(report)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polylens Telegram Console SRE Health Check")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument("--no-alert", action="store_true", help="Suppress Telegram alert on critical conditions")
    args = parser.parse_args(argv)

    report = run_check(suppress_alert=args.no_alert)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"status={report.status}")
        for check in report.checks:
            level = check.status.upper()
            print(f"  [{level}] {check.name}: {check.message}")
        if report.metrics:
            print(f"  metrics: uptime={report.metrics.get('uptime_human', 'N/A')} "
                  f"memory={report.metrics.get('memory_mb', 'N/A')}MB "
                  f"restarts={report.metrics.get('restart_count', 0)} "
                  f"warnings={report.metrics.get('warning_count', 0)} "
                  f"errors={report.metrics.get('error_count', 0)}")

    # Exit codes: 0=healthy, 1=warning, 2=critical
    if report.status == "critical":
        return 2
    if report.status == "warning":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())