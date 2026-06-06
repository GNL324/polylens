from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from src.storage.opportunities import alert_metrics, get_active_scanner_profile, latest_prop_scan_status, operations_metrics, prop_watch_health
from src.web.redaction import redact_mapping

ALLOWED_COMMANDS: Final[dict[str, list[str]]] = {
    "scan-live-arb": [
        "/home/noel/.venv/bin/python",
        "-m",
        "src.cli",
        "scan-live-arb",
        "--sport",
        "basketball_nba",
        "--json",
        "--save",
    ],
    "scan-prop-arb": [
        "/home/noel/.venv/bin/python",
        "-m",
        "src.cli",
        "scan-prop-arb",
        "--profile",
        "__ACTIVE__",
        "--json",
    ],
    "systemd-status": ["systemctl", "status", "polylens-live-arb.service", "--no-pager"],
    "dashboard-status": ["systemctl", "status", "polylens-dashboard.service", "--no-pager"],
    "live-arb-active": ["systemctl", "is-active", "polylens-live-arb.service"],
    "dashboard-active": ["systemctl", "is-active", "polylens-dashboard.service"],
    "telegram-test-alert": ["/home/noel/.venv/bin/python", "-m", "src.cli", "telegram-test-alert", "--json"],
    "prop-watch-start": ["systemctl", "start", "polylens-prop-watch.service"],
    "prop-watch-stop": ["systemctl", "stop", "polylens-prop-watch.service"],
    "prop-watch-restart": ["systemctl", "restart", "polylens-prop-watch.service"],
    "prop-watch-status": ["systemctl", "status", "polylens-prop-watch.service", "--no-pager"],
    "prop-watch-logs": ["journalctl", "-u", "polylens-prop-watch.service", "-n", "100", "--no-pager"],
}

PROFILE_REQUIRED_COMMANDS: Final[set[str]] = {"scan-prop-arb", "prop-watch-start", "prop-watch-restart"}


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        return redact_mapping(self.__dict__)


def allowed_command_names() -> list[str]:
    return list(ALLOWED_COMMANDS)


def get_allowed_command(name: str) -> list[str]:
    if name not in ALLOWED_COMMANDS:
        raise ValueError(f"blocked command: {name}")
    return list(ALLOWED_COMMANDS[name])


def run_allowed_command(name: str, timeout_seconds: int = 120) -> CommandResult:
    if name in PROFILE_REQUIRED_COMMANDS and get_active_scanner_profile() is None:
        raise ValueError("No active scanner profile configured.")
    command = get_allowed_command(name)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, shell=False)
    return CommandResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout[-6000:],
        stderr=completed.stderr[-6000:],
    )


def automation_status(db_path: str = "data/opportunities.db") -> dict[str, object]:
    live = run_allowed_command("live-arb-active", timeout_seconds=15)
    dashboard = run_allowed_command("dashboard-active", timeout_seconds=15)
    metrics = alert_metrics(db_path)
    operations = operations_metrics(db_path)
    latest_scan = latest_prop_scan_status(db_path)
    return {
        "polylens-live-arb.service": _active_label(live),
        "polylens-dashboard.service": _active_label(dashboard),
        "last_scan_time": latest_scan.get("started_at") if latest_scan else None,
        "last_successful_scan": operations.get("last_successful_scan"),
        "last_failed_scan": operations.get("last_failed_scan"),
        "last_alert_sent": metrics.get("last_alert_time"),
        "duplicate_alerts_suppressed_today": metrics.get("duplicates_suppressed_today", 0),
        "failed_alerts_today": metrics.get("failed_alerts_today", 0),
        "opportunities_found_today": operations.get("opportunities_today", 0),
        "scanner_uptime": _extract_uptime(run_allowed_command("systemd-status", timeout_seconds=20).stdout),
    }


def prop_watch_service_card(db_path: str = "data/opportunities.db") -> dict[str, object]:
    status = _safe_status("prop-watch-status")
    health = prop_watch_health(db_path)
    return {
        "active": _service_state_from_status(status),
        "last_scan_time": health.get("last_scan_time"),
        "last_scan_duration": health.get("last_scan_duration"),
        "opportunities_found_last_scan": health.get("opportunities_found_last_scan"),
        "alerts_sent_today": health.get("alerts_sent_today"),
        "duplicates_skipped_today": health.get("duplicates_skipped_today"),
        "failures_today": health.get("failures_today"),
        "error_message": health.get("error_message"),
    }


def health_badges(db_path: str = "data/opportunities.db", now: datetime | None = None) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    latest_scan = latest_prop_scan_status(db_path)
    metrics = alert_metrics(db_path)
    live = _safe_active("live-arb-active")
    dashboard = _safe_active("dashboard-active")
    last_scan = _parse_time(latest_scan.get("started_at") if latest_scan else None)
    last_alert = _parse_time(metrics.get("last_alert_time"))
    return {
        "scanner": _service_badge(live),
        "dashboard": _service_badge(dashboard),
        "telegram": "Configured" if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID") else "Missing",
        "odds_api": "Configured" if os.environ.get("ODDS_API_KEY") else "Missing",
        "last_scan_age": _age_badge(last_scan, now, none_label="Stale"),
        "last_alert_age": _age_badge(last_alert, now, none_label="None"),
    }


def _safe_active(name: str) -> str:
    try:
        return _active_label(run_allowed_command(name, timeout_seconds=15))
    except Exception:
        return "failed"


def _safe_status(name: str) -> CommandResult | None:
    try:
        return run_allowed_command(name, timeout_seconds=20)
    except Exception:
        return None


def _active_label(result: CommandResult) -> str:
    text = (result.stdout or result.stderr or "").strip().lower()
    return text or ("failed" if result.returncode else "active")


def _service_badge(status: str) -> str:
    if status == "active":
        return "Running"
    if status in {"inactive", "deactivating"}:
        return "Stopped"
    return "Failed"


def _service_state_from_status(result: CommandResult | None) -> str:
    if result is None:
        return "unknown"
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "active: active" in text:
        return "active"
    if "active: inactive" in text:
        return "inactive"
    if "active: failed" in text:
        return "failed"
    return "active" if result.returncode == 0 else "inactive"


def _age_badge(value: datetime | None, now: datetime, *, none_label: str) -> str:
    if value is None:
        return none_label
    age = (now - value).total_seconds()
    return "OK" if age <= 3600 else "Stale"


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_uptime(status_text: str) -> str | None:
    for line in status_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Active:"):
            return stripped
    return None
