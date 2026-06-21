from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


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


def test_listener_health_requires_localhost_and_rejects_exposed():
    findings = sre.listener_health(["127.0.0.1:8787", "0.0.0.0:8788"])
    by_code = {finding.code: finding for finding in findings}
    assert by_code["dashboard_localhost_8787"].level == "info"
    assert by_code["dashboard_exposed_8788"].level == "alert"


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
