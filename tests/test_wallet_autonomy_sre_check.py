from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.deployment.drift_check import DEPLOYMENT_DRIFT_ALERT_CODE
from src.integrations.telegram_console import TelegramConsole, TelegramConsoleConfig, TelegramResponse


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wallet_autonomy_sre_check.py"
spec = importlib.util.spec_from_file_location("wallet_autonomy_sre_check", SCRIPT)
sre = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = sre
spec.loader.exec_module(sre)


def test_root_traders_db_absent_is_info(tmp_path):
    findings = sre.root_db_hygiene(tmp_path / "traders.db")
    assert findings[0].level == "info"
    assert findings[0].code == "root_traders_db_absent"


def test_root_traders_db_zero_bytes_removed_without_sqlite_open(tmp_path):
    root_db = tmp_path / "traders.db"
    root_db.write_bytes(b"")
    findings = sre.root_db_hygiene(root_db)
    assert findings[0].code == "root_traders_db_removed"
    assert not root_db.exists()


def test_canonical_traders_db_valid(tmp_path):
    db = tmp_path / "data" / "traders.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE wallet_service_state (cycle_name TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE wallet_service_cycle_runs (cycle_name TEXT, status TEXT, finished_at TEXT, error TEXT)")
    findings = sre.check_sqlite_db(db, code_prefix="canonical_traders_db")
    assert findings[0].level == "info"
    assert findings[0].code == "canonical_traders_db_ok"


def test_dashboard_redirect_and_mission_control_ok(monkeypatch):
    statuses = {
        "http://127.0.0.1:8787/": 307,
        "http://127.0.0.1:8787/mission-control": 200,
        "http://127.0.0.1:8788/": 200,
    }
    monkeypatch.setattr(sre, "raw_http_status", lambda url: statuses[url])
    findings = sre.dashboard_http_health()
    assert {finding.code for finding in findings} == {"dashboard_root_redirect_ok", "mission_control_ok", "trader_dashboard_ok"}
    assert all(finding.level == "info" for finding in findings)


def test_dashboard_http_failures_are_warnings_only(monkeypatch):
    monkeypatch.setattr(sre, "raw_http_status", lambda _url: None)
    findings = sre.dashboard_http_health()
    assert all(finding.level == "warning" for finding in findings)
    assert {finding.code for finding in findings} == {
        "dashboard_root_redirect_bad",
        "mission_control_bad",
        "trader_dashboard_bad",
    }


def test_listener_health_requires_localhost_and_rejects_exposed():
    findings = sre.listener_health(["127.0.0.1:8787", "0.0.0.0:8788"])
    by_code = {finding.code: finding for finding in findings}
    assert by_code["dashboard_localhost_8787"].level == "info"
    assert by_code["dashboard_exposed_8788"].level == "warning"


def test_old_errors_cleared_by_later_success_are_info(tmp_path):
    db = tmp_path / "traders.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE wallet_service_cycle_runs (cycle_name TEXT, status TEXT, finished_at TEXT, error TEXT)")
        conn.execute("INSERT INTO wallet_service_cycle_runs VALUES ('signals', 'error', '2026-06-21T10:00:00Z', 'UNIQUE constraint failed')")
        conn.execute("INSERT INTO wallet_service_cycle_runs VALUES ('signals', 'success', '2026-06-21T10:05:00Z', NULL)")
    findings = sre.recent_failure_findings(db, now=datetime(2026, 6, 21, 10, 30, tzinfo=timezone.utc), recent_window_minutes=30)
    assert any(finding.code == "historical_errors_resolved" for finding in findings)
    assert not any(finding.level == "alert" for finding in findings)


def test_recent_uncleared_error_alerts(tmp_path):
    db = tmp_path / "traders.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE wallet_service_cycle_runs (cycle_name TEXT, status TEXT, finished_at TEXT, error TEXT)")
        conn.execute("INSERT INTO wallet_service_cycle_runs VALUES ('signals', 'success', '2026-06-21T10:00:00Z', NULL)")
        conn.execute("INSERT INTO wallet_service_cycle_runs VALUES ('signals', 'error', '2026-06-21T10:10:00Z', 'database is locked')")
    findings = sre.recent_failure_findings(db, now=datetime(2026, 6, 21, 10, 30, tzinfo=timezone.utc), recent_window_minutes=30)
    assert any(finding.code == "recent_uncleared_wallet_service_error" and finding.level == "alert" for finding in findings)


def test_oneshot_inactive_success_is_healthy(monkeypatch):
    class Result:
        returncode = 0
        stdout = "ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n"
        stderr = ""

    monkeypatch.setattr(sre.subprocess, "run", lambda *args, **kwargs: Result())
    findings = sre.systemd_service_health()
    assert findings[0].code == "wallet_autonomy_oneshot_success"
    assert findings[0].level == "info"


def _healthy_telegram_console(tmp_path) -> TelegramConsole:
    config = TelegramConsoleConfig(
        bot_token="test-token",
        admin_user_ids=frozenset({123}),
        paper_only=True,
        live_enabled=False,
        audit_db_path=str(tmp_path / "telegram_audit.db"),
    )
    return TelegramConsole(
        config,
        health_provider=lambda: {"status": "healthy", "stale_cycles": [], "success_rate": 1.0},
        signals_provider=lambda: {"signal_count": 1, "validation_accuracy": 0.5, "last_cycle": {}},
        paper_provider=lambda: {"status": "healthy", "equity": 100.0},
        paper_intelligence_provider=lambda: {"trade_count": 0, "recent_trades": [], "open_positions": []},
        risk_provider=lambda: {"halted": False, "active_halts": []},
    )


def _passing_run_check_patches(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sre,
        "deployment_drift_report",
        lambda *_args, **_kwargs: {"findings": [{"level": "info", "code": "deployment_revision_ok", "message": "ok", "details": {}}]},
    )
    monkeypatch.setattr(sre, "root_db_hygiene", lambda **_kwargs: [])
    monkeypatch.setattr(sre, "check_sqlite_db", lambda *_args, **_kwargs: [sre.Finding("info", "canonical_traders_db_ok", "ok")])
    monkeypatch.setattr(sre, "listener_health", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sre, "dashboard_http_health", lambda: [sre.Finding("warning", "trader_dashboard_bad", "down")])
    monkeypatch.setattr(
        sre,
        "wallet_service_health",
        lambda: [sre.Finding("info", "wallet_service_health_ok", "ok")],
    )
    monkeypatch.setattr(
        sre,
        "systemd_service_health",
        lambda **_kwargs: [sre.Finding("info", "wallet_autonomy_oneshot_success", "ok")],
    )
    monkeypatch.setattr(sre, "recent_failure_findings", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        sre,
        "telegram_console_health",
        lambda **_kwargs: [sre.Finding("info", "telegram_console_service_running", "ok")],
    )


def test_dashboard_down_with_healthy_telegram_does_not_alert(monkeypatch, tmp_path):
    _passing_run_check_patches(monkeypatch, tmp_path)
    report = sre.run_check()
    assert report["alert_count"] == 0
    assert report["warning_count"] >= 1
    assert report["status"] == "healthy"


def test_telegram_service_down_alerts(monkeypatch):
    class ActiveResult:
        returncode = 0
        stdout = "inactive"
        stderr = ""

    def fake_run(args, **_kwargs):
        if args[:2] == ["systemctl", "is-active"]:
            return ActiveResult()
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sre.subprocess, "run", fake_run)
    monkeypatch.setattr(sre, "_systemctl_show", lambda _unit: {"SubState": "dead", "MainPID": "0"})
    findings = sre.telegram_service_health()
    assert any(finding.code == "telegram_console_service_inactive" and finding.level == "alert" for finding in findings)


def test_telegram_page_render_failure_alerts(tmp_path):
    console = _healthy_telegram_console(tmp_path)

    def fail_callback(*_args, **_kwargs):
        return TelegramResponse("error: command failed safely")

    console.handle_console_callback = fail_callback  # type: ignore[method-assign]
    findings = sre.telegram_page_render_health(console=console)
    assert any(finding.level == "alert" and finding.code.endswith("_failed") for finding in findings)


def test_unsafe_telegram_live_flags_alert():
    env = {
        "POLYLENS_TELEGRAM_ADMIN_USER_IDS": "123",
        "POLYLENS_TELEGRAM_PAPER_ONLY": "false",
        "POLYLENS_TELEGRAM_LIVE_ENABLED": "true",
    }
    findings = sre.telegram_config_safety(env=env)
    codes = {finding.code for finding in findings if finding.level == "alert"}
    assert "telegram_paper_only_disabled" in codes
    assert "telegram_live_enabled" in codes


def test_deployment_drift_alerts(monkeypatch, tmp_path):
    _passing_run_check_patches(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sre,
        "deployment_drift_report",
        lambda *_args, **_kwargs: {
            "findings": [
                {
                    "level": "alert",
                    "code": DEPLOYMENT_DRIFT_ALERT_CODE,
                    "message": "drift",
                    "details": {},
                }
            ]
        },
    )
    report = sre.run_check()
    assert report["alert_count"] == 1
    assert any(finding["code"] == DEPLOYMENT_DRIFT_ALERT_CODE for finding in report["findings"])


def test_wallet_autonomy_bad_state_alerts(monkeypatch):
    class Result:
        returncode = 0
        stdout = "ActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=1\n"
        stderr = ""

    monkeypatch.setattr(sre.subprocess, "run", lambda *args, **kwargs: Result())
    findings = sre.systemd_service_health()
    assert any(finding.code == "wallet_autonomy_service_bad_state" and finding.level == "alert" for finding in findings)


def test_telegram_page_render_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.analysis.opportunity_feed.get_paper_trading_opportunities",
        lambda **_kwargs: [],
    )
    console = _healthy_telegram_console(tmp_path)
    findings = sre.telegram_page_render_health(console=console)
    assert len(findings) == len(sre.TELEGRAM_CONSOLE_PAGES)
    assert all(finding.level == "info" for finding in findings)
