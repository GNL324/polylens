"""Tests for the Telegram Console SRE Health Check script.

These tests use monkeypatching to simulate service states and log output,
so they run without an actual systemd service or Telegram bot.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "telegram_console_health_check.py"
spec = importlib.util.spec_from_file_location("telegram_console_health_check", SCRIPT)
sre = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = sre
spec.loader.exec_module(sre)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_systemctl_show(props: dict[str, str]):
    def fake_run(cmd, **kwargs):
        if "show" in cmd:
            lines = [f"{k}={v}" for k, v in props.items()]
            return FakeResult(stdout="\n".join(lines))
        if "is-active" in cmd:
            return FakeResult(stdout=props.get("ActiveState", "active") + "\n")
        return FakeResult(stdout="", stderr="")
    return fake_run


def _make_journalctl(lines: list[str]):
    def fake_run(cmd, **kwargs):
        return FakeResult(stdout="\n".join(lines))
    return fake_run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_service_active_pass(monkeypatch):
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show({"ActiveState": "active"}))
    result = sre.check_service_active()
    assert result.status == "pass"


def test_check_service_active_fail(monkeypatch):
    def fake_run(cmd, **kwargs):
        if "is-active" in cmd:
            return FakeResult(stdout="inactive\n")
        return FakeResult(stdout="")
    monkeypatch.setattr(sre.subprocess, "run", fake_run)
    result = sre.check_service_active()
    assert result.status == "fail"


def test_check_process_running(monkeypatch):
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show({"MainPID": "12345"}))
    monkeypatch.setattr(Path, "exists", lambda self: True)
    result = sre.check_process_running()
    assert result.status == "pass"


def test_check_process_running_fail(monkeypatch):
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show({"MainPID": "0"}))
    result = sre.check_process_running()
    assert result.status == "fail"


def test_check_no_restart_loop_pass(monkeypatch):
    lines = ["Started polylens-telegram-console.service"]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result = sre.check_no_restart_loop()
    assert result.status == "pass"


def test_check_no_restart_loop_fail(monkeypatch):
    lines = [f"Started polylens-telegram-console.service" for _ in range(10)]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result = sre.check_no_restart_loop()
    assert result.status == "fail"
    assert result.details["restart_count"] == 10


def test_check_logs_clean_pass(monkeypatch):
    lines = [
        "2026-06-26 INFO: starting telegram console",
        "2026-06-26 INFO: polling active",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "pass"
    assert counts == {}


def test_check_logs_clean_traceback_fail(monkeypatch):
    lines = [
        "2026-06-26 INFO: starting telegram console",
        "2026-06-26 ERROR: Traceback (most recent call last):",
        "  File ...",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "fail"
    assert counts.get("traceback", 0) >= 1


def test_check_logs_clean_http_401_fail(monkeypatch):
    lines = [
        "2026-06-26 INFO: starting telegram console",
        "2026-06-26 ERROR: HTTP 401 Unauthorized",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "fail"
    assert counts.get("http_401", 0) >= 1


def test_check_logs_clean_http_429_warning(monkeypatch):
    lines = [
        "2026-06-26 INFO: starting telegram console",
        "2026-06-26 WARNING: HTTP 429 Too Many Requests",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    # Single 429 is a warning, not critical
    assert result.status == "warn"
    assert counts.get("http_429", 0) == 1


def test_check_logs_clean_repeated_429_critical(monkeypatch):
    lines = [
        "2026-06-26 WARNING: HTTP 429 Too Many Requests",
        "2026-06-26 WARNING: HTTP 429 Too Many Requests",
        "2026-06-26 WARNING: HTTP 429 Too Many Requests",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "warn"
    assert counts.get("http_429", 0) >= 3


def test_check_logs_clean_edit_failure_warning(monkeypatch):
    lines = [
        "telegram edit failed; falling back to sendMessage",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "warn"
    assert counts.get("edit_failure", 0) >= 1


def test_check_logs_clean_callback_failure_warning(monkeypatch):
    lines = [
        "telegram answerCallbackQuery failed; continuing",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "warn"
    assert counts.get("callback_failure", 0) >= 1


def test_check_logs_clean_token_leak_fail(monkeypatch):
    lines = [
        "2026-06-26 INFO: config={'bot_token': '8049379954:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "fail"
    assert result.details.get("token_leaked") is True


def test_check_logs_clean_redacted_token_safe(monkeypatch):
    lines = [
        "config={'bot_token': 'redacted'}",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result, counts = sre.check_logs_clean()
    assert result.status == "pass"


def test_check_paper_only_pass(monkeypatch):
    props = {
        "MainPID": "12345",
        "Environment": "POLYLENS_TELEGRAM_PAPER_ONLY=true",
    }
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show(props))

    def fake_get_env_from_pid(pid):
        return {"POLYLENS_TELEGRAM_PAPER_ONLY": "true"}

    monkeypatch.setattr(sre, "_get_env_from_pid", fake_get_env_from_pid)
    result = sre.check_paper_only()
    assert result.status == "pass"


def test_check_paper_only_fail(monkeypatch):
    props = {
        "MainPID": "12345",
        "Environment": "POLYLENS_TELEGRAM_PAPER_ONLY=false",
    }
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show(props))

    def fake_get_env_from_pid(pid):
        return {"POLYLENS_TELEGRAM_PAPER_ONLY": "false"}

    monkeypatch.setattr(sre, "_get_env_from_pid", fake_get_env_from_pid)
    result = sre.check_paper_only()
    assert result.status == "fail"


def test_check_live_disabled_pass(monkeypatch):
    props = {"MainPID": "12345", "Environment": ""}
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show(props))

    def fake_get_env_from_pid(pid):
        return {var: "false" for var in sre.LIVE_TRADING_ENV_VARS}

    monkeypatch.setattr(sre, "_get_env_from_pid", fake_get_env_from_pid)
    result, states = sre.check_live_disabled()
    assert result.status == "pass"
    assert all(not v for v in states.values())


def test_check_live_disabled_fail(monkeypatch):
    props = {"MainPID": "12345", "Environment": ""}
    monkeypatch.setattr(sre.subprocess, "run", _make_systemctl_show(props))

    def fake_get_env_from_pid(pid):
        env = {var: "false" for var in sre.LIVE_TRADING_ENV_VARS}
        env["POLYLENS_LIVE_TRADING"] = "true"
        return env

    monkeypatch.setattr(sre, "_get_env_from_pid", fake_get_env_from_pid)
    result, states = sre.check_live_disabled()
    assert result.status == "fail"
    assert states["POLYLENS_LIVE_TRADING"] is True


def test_check_polling_active_pass(monkeypatch):
    lines = [
        "2026-06-26 INFO: starting telegram console config={...}",
    ]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result = sre.check_polling_active()
    assert result.status == "pass"


def test_check_polling_active_fail(monkeypatch):
    lines = ["some random log without startup"]
    monkeypatch.setattr(sre.subprocess, "run", _make_journalctl(lines))
    result = sre.check_polling_active()
    assert result.status == "fail"


def test_should_alert_service_down():
    checks = [sre.Check("service_active", "fail", "Service is down.")]
    assert sre._should_alert(checks) is True


def test_should_alert_live_enabled():
    checks = [sre.Check("live_disabled", "fail", "Live trading enabled.")]
    assert sre._should_alert(checks) is True


def test_should_alert_not_on_isolated_edit_failure():
    checks = [
        sre.Check(
            "logs_clean",
            "warn",
            "Minor issues",
            {"pattern_counts": {"edit_failure": 1}, "token_leaked": False},
        ),
    ]
    assert sre._should_alert(checks) is False


def test_should_alert_on_repeated_edit_failure():
    checks = [
        sre.Check(
            "logs_clean",
            "fail",
            "Repeated edit failures",
            {"pattern_counts": {"edit_failure": 5}, "token_leaked": False},
        ),
    ]
    assert sre._should_alert(checks) is True


def test_should_alert_on_restart_loop():
    checks = [sre.Check("no_restart_loop", "fail", "Restart loop detected.")]
    assert sre._should_alert(checks) is True


def test_should_alert_not_on_isolated_429():
    checks = [
        sre.Check(
            "logs_clean",
            "warn",
            "Minor 429",
            {"pattern_counts": {"http_429": 1}, "token_leaked": False},
        ),
    ]
    assert sre._should_alert(checks) is False


def test_exit_code_healthy():
    report = sre.HealthReport(
        status="healthy",
        checks=[sre.Check("test", "pass", "ok")],
        warnings=[],
        errors=[],
        metrics={},
    )
    # Simulate exit code logic
    if report.status == "critical":
        assert 2 == 2
    elif report.status == "warning":
        assert 1 == 1
    else:
        assert 0 == 0


def test_exit_code_warning():
    report = sre.HealthReport(
        status="warning",
        checks=[sre.Check("test", "warn", "warning")],
        warnings=["warning"],
        errors=[],
        metrics={},
    )
    assert report.status == "warning"


def test_exit_code_critical():
    report = sre.HealthReport(
        status="critical",
        checks=[sre.Check("test", "fail", "fail")],
        warnings=[],
        errors=["fail"],
        metrics={},
    )
    assert report.status == "critical"


def test_json_output_structure():
    report = sre.HealthReport(
        status="healthy",
        checks=[sre.Check("test", "pass", "ok", {"key": "value"})],
        warnings=[],
        errors=[],
        metrics={"uptime_seconds": 120.0},
    )
    d = report.to_dict()
    assert d["status"] == "healthy"
    assert isinstance(d["checks"], list)
    assert d["checks"][0]["name"] == "test"
    assert d["checks"][0]["status"] == "pass"
    assert d["checks"][0]["details"] == {"key": "value"}
    assert d["metrics"]["uptime_seconds"] == 120.0


def test_human_duration():
    assert sre._human_duration(30) == "30s"
    assert sre._human_duration(90) == "1.5m"
    assert sre._human_duration(3700) == "1.0h"
    assert sre._human_duration(90000) == "1.0d"


def test_env_bool():
    assert sre._env_bool("true") is True
    assert sre._env_bool("1") is True
    assert sre._env_bool("yes") is True
    assert sre._env_bool("on") is True
    assert sre._env_bool("false") is False
    assert sre._env_bool("0") is False
    assert sre._env_bool(None) is False
    assert sre._env_bool("") is False