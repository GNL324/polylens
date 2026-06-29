from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.integrations import sre_health
from src.integrations.telegram_sre import (
    alert_fingerprint,
    build_sre_report,
    format_sre_alert_telegram,
    format_sre_status_telegram,
    load_alert_state,
    save_alert_state,
    send_proactive_sre_alert,
    should_send_proactive_alert,
    update_alert_state,
)


def _sample_report(*, status: str = "alert", alert_count: int = 1, warning_count: int = 0) -> dict:
    return {
        "status": status,
        "alert_count": alert_count,
        "warning_count": warning_count,
        "checked_at": "2026-06-28T12:00:00Z",
        "deployment_drift": {"status": "healthy", "message": "ok"},
        "wallet_service_health": {"status": "healthy", "message": "ok"},
        "recommended_actions": ["Inspect wallet-autonomy.service logs and wait for the next signals cycle."],
        "findings": [
            {
                "level": "alert",
                "code": "wallet_service_health_unhealthy",
                "message": "wallet-service-health reported unhealthy/degraded state.",
                "details": {},
            }
        ],
        "paper_only": True,
        "read_only": True,
    }


def test_format_sre_status_telegram_renders_sections():
    text = format_sre_status_telegram(_sample_report())
    assert "🛡️ Ops Health" in text
    assert "Status: ALERT" in text
    assert "Alerts: 1 | Warnings: 0" in text
    assert "Deployment Drift: healthy" in text
    assert "Wallet Service: healthy" in text
    assert "[wallet_service_health_unhealthy]" in text
    assert "Inspect wallet-autonomy.service logs" in text
    assert "Paper-only | Read-only" in text


def test_format_sre_alert_telegram_uses_recovery_title():
    alert_text = format_sre_alert_telegram(_sample_report(), reason="alert")
    recovered_text = format_sre_alert_telegram(_sample_report(status="healthy", alert_count=0), reason="recovered")
    assert "SRE Alert" in alert_text
    assert "SRE Recovered" in recovered_text
    assert "Ops Health" in alert_text


def test_alert_fingerprint_is_stable():
    report = _sample_report()
    assert alert_fingerprint(report) == alert_fingerprint(report)
    changed = dict(report)
    changed["findings"] = list(report["findings"]) + [
        {"level": "warning", "code": "deployment_drift_unknown", "message": "drift", "details": {}}
    ]
    assert alert_fingerprint(report) != alert_fingerprint(changed)


@pytest.mark.parametrize(
    ("report", "state", "expected_send", "expected_reason"),
    [
        (_sample_report(status="healthy", alert_count=0, warning_count=0), {}, False, "healthy_silent"),
        (_sample_report(), {}, True, "alert_or_warning"),
        (_sample_report(), {"last_sent_fingerprint": alert_fingerprint(_sample_report())}, False, "duplicate"),
        (
            _sample_report(status="healthy", alert_count=0, warning_count=0),
            {"last_status": "alert", "last_sent_fingerprint": "alert|alert:wallet_service_health_unhealthy"},
            True,
            "recovered",
        ),
    ],
)
def test_should_send_proactive_alert(report, state, expected_send, expected_reason):
    should_send, reason = should_send_proactive_alert(report, state=state)
    assert should_send is expected_send
    assert reason == expected_reason


def test_update_alert_state_tracks_sent_fingerprint():
    report = _sample_report()
    state = update_alert_state(report, sent=True)
    assert state["last_status"] == "alert"
    assert state["last_fingerprint"] == alert_fingerprint(report)
    assert state["last_sent_fingerprint"] == state["last_fingerprint"]


def test_alert_state_file_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = {"last_status": "alert", "last_fingerprint": "alert|alert:foo"}
    save_alert_state(state, path)
    loaded = load_alert_state(path)
    assert loaded == state


def test_send_proactive_sre_alert_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_TELEGRAM_SRE_ALERT_ENABLED", "true")
    report = _sample_report()
    result = send_proactive_sre_alert(report, state_path=tmp_path / "state.json", dry_run=True)
    assert result["should_send"] is True
    assert result["delivery_status"] == "dry_run"
    assert "SRE Alert" in result["text"]


def test_send_proactive_sre_alert_respects_disable_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_TELEGRAM_SRE_ALERT_ENABLED", "false")
    report = _sample_report()
    result = send_proactive_sre_alert(report, state_path=tmp_path / "state.json")
    assert result["should_send"] is True
    assert result["delivery_status"] == "disabled"


def test_build_sre_report_smoke_json_shape(monkeypatch):
    fixed_now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(sre_health, "root_db_hygiene", lambda **kwargs: [])
    monkeypatch.setattr(sre_health, "check_sqlite_db", lambda *args, **kwargs: [sre_health.Finding("info", "canonical_traders_db_ok", "ok")])
    monkeypatch.setattr(
        sre_health,
        "deployment_drift_report",
        lambda *_args, **_kwargs: {"findings": [{"level": "info", "code": "deployment_revision_ok", "message": "ok", "details": {}}]},
    )
    monkeypatch.setattr(sre_health, "listener_health", lambda listeners: [sre_health.Finding("info", "dashboard_localhost_8787", "ok")])
    monkeypatch.setattr(sre_health, "dashboard_http_health", lambda: [sre_health.Finding("info", "mission_control_ok", "ok")])
    monkeypatch.setattr(sre_health, "wallet_service_health", lambda **kwargs: [sre_health.Finding("info", "wallet_service_health_ok", "ok", {"status": "healthy"})])
    monkeypatch.setattr(sre_health, "systemd_service_health", lambda **kwargs: [sre_health.Finding("info", "wallet_autonomy_oneshot_success", "ok")])
    monkeypatch.setattr(sre_health, "telegram_console_health", lambda **kwargs: [sre_health.Finding("info", "telegram_console_service_running", "ok")])
    monkeypatch.setattr(sre_health, "recent_failure_findings", lambda *args, **kwargs: [sre_health.Finding("info", "no_recent_uncleared_errors", "ok")])

    report = build_sre_report(remove_root_artifact=False, now=fixed_now)
    payload = json.loads(json.dumps(report))
    assert payload["status"] == "healthy"
    assert payload["alert_count"] == 0
    assert payload["checked_at"] == "2026-06-28T12:00:00Z"
    assert payload["deployment_drift"]["status"] == "healthy"
    assert payload["wallet_service_health"]["status"] == "healthy"
    assert payload["recommended_actions"] == ["No action required."]
