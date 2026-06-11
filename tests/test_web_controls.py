import subprocess
from datetime import datetime, timezone

import pytest

from src.storage.opportunities import record_alert_event, save_prop_arbitrage_scan_result
from src.web.controls import ALLOWED_COMMANDS, CommandResult, get_allowed_command, health_badges, prop_watch_service_card, run_allowed_command


def test_allowed_bot_control_commands_are_strict() -> None:
    assert get_allowed_command("scan-live-arb") == [
        "/home/noel/.venv/bin/python",
        "-m",
        "src.cli",
        "scan-live-arb",
        "--sport",
        "basketball_nba",
        "--json",
        "--save",
    ]
    assert get_allowed_command("scan-prop-arb") == [
        "/home/noel/.venv/bin/python",
        "-m",
        "src.cli",
        "scan-prop-arb",
        "--profile",
        "__ACTIVE__",
        "--json",
    ]
    assert get_allowed_command("systemd-status") == ["systemctl", "status", "polylens-live-arb.service", "--no-pager"]
    assert get_allowed_command("dashboard-status") == ["systemctl", "status", "polylens-dashboard.service", "--no-pager"]
    assert get_allowed_command("live-arb-active") == ["systemctl", "is-active", "polylens-live-arb.service"]
    assert get_allowed_command("dashboard-active") == ["systemctl", "is-active", "polylens-dashboard.service"]
    assert get_allowed_command("telegram-test-alert") == ["/home/noel/.venv/bin/python", "-m", "src.cli", "telegram-test-alert", "--json"]
    assert get_allowed_command("prop-watch-start") == ["systemctl", "start", "polylens-prop-watch.service"]
    assert get_allowed_command("prop-watch-stop") == ["systemctl", "stop", "polylens-prop-watch.service"]
    assert get_allowed_command("prop-watch-restart") == ["systemctl", "restart", "polylens-prop-watch.service"]
    assert get_allowed_command("prop-watch-status") == ["systemctl", "status", "polylens-prop-watch.service", "--no-pager"]
    assert get_allowed_command("prop-watch-logs") == ["journalctl", "-u", "polylens-prop-watch.service", "-n", "100", "--no-pager"]
    assert all(isinstance(command, list) for command in ALLOWED_COMMANDS.values())


def test_blocked_arbitrary_commands() -> None:
    with pytest.raises(ValueError):
        get_allowed_command("rm -rf /")


def test_old_generic_commands_are_not_allowlisted() -> None:
    assert ["python", "-m", "src.cli", "scan-live-arb", "--json", "--save"] not in ALLOWED_COMMANDS.values()
    assert ["/home/noel/.venv/bin/python", "-m", "src.cli", "scan-live-arb", "--json", "--save"] not in ALLOWED_COMMANDS.values()
    assert ["/home/noel/.venv/bin/python", "-m", "src.cli", "scan-prop-arb", "--json"] not in ALLOWED_COMMANDS.values()


def test_run_allowed_command_uses_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_allowed_command("systemd-status")
    assert result.returncode == 0
    assert calls["command"] == ["systemctl", "status", "polylens-live-arb.service", "--no-pager"]
    assert calls["kwargs"]["shell"] is False


def test_profile_required_command_blocks_missing_active_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.web.controls.get_active_scanner_profile", lambda: None)
    with pytest.raises(ValueError, match="No active scanner profile"):
        run_allowed_command("scan-prop-arb")


def test_health_badges_calculate_config_and_age(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "opportunities.db"
    fixed_now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    save_prop_arbitrage_scan_result(
        {"prop_arbitrage_candidates": [], "scan_duration_seconds": 0.5},
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at=fixed_now.isoformat(),
    )
    record_alert_event(
        db_path=str(db_path),
        created_at=fixed_now.isoformat(),
        alert_type="prop_arbitrage",
        channel="telegram",
        status="sent",
        reason="send success",
    )

    def fake_run(name: str, timeout_seconds: int = 120) -> CommandResult:
        return CommandResult(name=name, command=[name], returncode=0, stdout="active\n", stderr="")

    monkeypatch.setattr("src.web.controls.run_allowed_command", fake_run)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("ODDS_API_KEY", "odds-secret")

    badges = health_badges(str(db_path), now=fixed_now)
    assert badges == {
        "scanner": "Running",
        "dashboard": "Running",
        "telegram": "Configured",
        "odds_api": "Configured",
        "last_scan_age": "OK",
        "last_alert_age": "OK",
    }


def test_health_badges_missing_and_stale(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "opportunities.db"
    fixed_now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
    save_prop_arbitrage_scan_result(
        {"prop_arbitrage_candidates": [], "scan_duration_seconds": 0.5},
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at=fixed_now.replace(hour=8).isoformat(),
    )

    def fake_run(name: str, timeout_seconds: int = 120) -> CommandResult:
        return CommandResult(name=name, command=[name], returncode=3, stdout="inactive\n", stderr="")

    monkeypatch.setattr("src.web.controls.run_allowed_command", fake_run)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    badges = health_badges(str(db_path), now=fixed_now)
    assert badges["scanner"] == "Stopped"
    assert badges["dashboard"] == "Stopped"
    assert badges["telegram"] == "Missing"
    assert badges["odds_api"] == "Missing"
    assert badges["last_scan_age"] == "Stale"
    assert badges["last_alert_age"] == "None"


def test_prop_watch_service_card_uses_allowlisted_status_and_metrics(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "opportunities.db"
    today = datetime.now(timezone.utc).date()
    fixed_now = datetime(today.year, today.month, today.day, 11, 30, tzinfo=timezone.utc)
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 10,
            "matched_prop_pairs": 4,
            "rejected_pairs": 2,
            "scan_duration_seconds": 1.25,
            "prop_arbitrage_candidates": [],
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at=fixed_now.replace(hour=11, minute=30).isoformat(),
        finished_at=fixed_now.replace(hour=11, minute=30, second=1).isoformat(),
    )
    record_alert_event(db_path=str(db_path), created_at=(fixed_now.replace(minute=45)).isoformat(), status="sent")
    record_alert_event(db_path=str(db_path), created_at=(fixed_now.replace(minute=46)).isoformat(), status="duplicate")
    record_alert_event(db_path=str(db_path), created_at=(fixed_now.replace(minute=47)).isoformat(), status="failed")

    def fake_run(name: str, timeout_seconds: int = 120) -> CommandResult:
        assert name == "prop-watch-status"
        return CommandResult(name=name, command=get_allowed_command(name), returncode=0, stdout="Active: active (running)\n", stderr="")

    monkeypatch.setattr("src.web.controls.run_allowed_command", fake_run)

    card = prop_watch_service_card(str(db_path))
    assert card["active"] == "active"
    assert card["last_scan_time"] == fixed_now.replace(hour=11, minute=30).isoformat()
    assert card["last_scan_duration"] == 1.25
    assert card["opportunities_found_last_scan"] == 0
    assert card["alerts_sent_today"] == 1
    assert card["duplicates_skipped_today"] == 1
    assert card["failures_today"] == 1
