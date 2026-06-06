from __future__ import annotations

import json
from datetime import datetime, timezone

from src.cli import telegram_test_alert
from src.storage.opportunities import alert_metrics, latest_prop_scan_status, load_alert_events, prop_watch_health, record_alert_event, save_prop_arbitrage_scan_result
from src.web.dashboard import alert_rows


def test_alert_event_persistence_and_secret_redaction(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    event_id = record_alert_event(
        db_path=str(db_path),
        created_at="2026-06-05T10:00:00+00:00",
        alert_type="prop_arbitrage",
        channel="telegram",
        player="Mitchell Robinson",
        market_title="player_points",
        roi=0.04,
        profit=40,
        status="sent",
        reason="send success",
        raw={"TELEGRAM_BOT_TOKEN": "super-secret", "nested": {"ODDS_API_KEY": "odds-secret"}, "safe": "visible"},
    )

    rows = load_alert_events(db_path=str(db_path))
    assert event_id == 1
    assert rows[0]["status"] == "sent"
    raw = json.loads(rows[0]["raw_json"])
    assert raw["TELEGRAM_BOT_TOKEN"] == "[REDACTED]"
    assert raw["nested"]["ODDS_API_KEY"] == "[REDACTED]"
    assert raw["safe"] == "visible"
    assert "super-secret" not in rows[0]["raw_json"]
    assert "odds-secret" not in rows[0]["raw_json"]


def test_duplicate_and_failed_alert_event_metrics(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    record_alert_event(db_path=str(db_path), created_at="2026-06-05T10:00:00+00:00", status="duplicate", reason="duplicate suppression")
    record_alert_event(db_path=str(db_path), created_at="2026-06-05T10:05:00+00:00", status="failed", reason="webhook failed")
    record_alert_event(db_path=str(db_path), created_at="2026-06-05T10:10:00+00:00", status="sent", roi=0.06, profit=60, player="A")

    metrics = alert_metrics(str(db_path), now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc))
    assert metrics["alerts_sent_today"] == 1
    assert metrics["duplicates_suppressed_today"] == 1
    assert metrics["failed_alerts_today"] == 1
    assert metrics["last_alert_time"] == "2026-06-05T10:10:00+00:00"
    assert metrics["average_alert_roi_today"] == 0.06
    assert metrics["top_alerting_opportunity_today"]["player"] == "A"


def test_telegram_test_alert_records_missing_config(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "opportunities.db"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    result = telegram_test_alert(as_json=False, db_path=str(db_path))

    assert result["sent"] is False
    assert result["status"] == "skipped"
    assert result["reason"] == "missing Telegram config"
    rows = load_alert_events(db_path=str(db_path))
    assert rows[0]["alert_type"] == "telegram_test"
    assert rows[0]["channel"] == "telegram"
    assert rows[0]["status"] == "skipped"
    assert rows[0]["reason"] == "missing Telegram config"


def test_dashboard_alert_rows_format_roi(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    record_alert_event(
        db_path=str(db_path),
        created_at="2026-06-05T10:00:00+00:00",
        alert_type="prop_arbitrage",
        channel="telegram",
        player="Mitchell Robinson",
        market_title="player_points",
        roi=0.05,
        profit=50,
        status="sent",
        reason="send success",
    )

    rows = alert_rows(str(db_path))
    assert rows[0]["player"] == "Mitchell Robinson"
    assert rows[0]["roi_percent"] == "5.00%"


def test_operations_metrics_include_alert_events(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {"prop_arbitrage_candidates": [], "scan_duration_seconds": 0.5},
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
    )
    record_alert_event(db_path=str(db_path), created_at="2026-06-05T10:15:00+00:00", status="duplicate")
    record_alert_event(db_path=str(db_path), created_at="2026-06-05T10:20:00+00:00", status="failed")
    record_alert_event(db_path=str(db_path), created_at="2026-06-05T10:25:00+00:00", status="sent")

    from src.storage.opportunities import operations_metrics

    metrics = operations_metrics(str(db_path), now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc))
    assert metrics["duplicate_alerts_suppressed_today"] == 1
    assert metrics["failed_alerts_today"] == 1
    assert metrics["last_alert_sent"] == "2026-06-05T10:25:00+00:00"


def test_scanner_health_persistence_records_finish_duplicates_and_saved_ids(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    opportunity = {
        "player": "Mitchell Robinson",
        "prop_type": "player_points",
        "line": 2.5,
        "over_book": "BetRivers",
        "over_odds": -139,
        "under_book": "Bovada",
        "under_odds": 170,
        "guaranteed_roi": 0.05042,
        "guaranteed_profit_amount": 50.42,
    }
    _, first_ids = save_prop_arbitrage_scan_result(
        {
            "props_fetched": 20,
            "matched_prop_pairs": 6,
            "rejected_pairs": 4,
            "scan_duration_seconds": 1.5,
            "prop_arbitrage_candidates": [opportunity],
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
        finished_at="2026-06-05T10:00:02+00:00",
    )
    _, duplicate_ids = save_prop_arbitrage_scan_result(
        {
            "props_fetched": 20,
            "matched_prop_pairs": 6,
            "rejected_pairs": 4,
            "scan_duration_seconds": 1.25,
            "prop_arbitrage_candidates": [opportunity],
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:05:00+00:00",
        finished_at="2026-06-05T10:05:01+00:00",
    )

    assert first_ids == [1]
    assert duplicate_ids == []
    latest = latest_prop_scan_status(str(db_path))
    assert latest is not None
    assert latest["finished_at"] == "2026-06-05T10:05:01+00:00"
    assert latest["duplicate_count"] == 1
    assert latest["saved_opportunity_ids"] == "[]"
    assert latest["error_message"] is None


def test_scanner_health_persistence_records_failures(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 0,
            "matched_prop_pairs": 0,
            "rejected_pairs": 0,
            "scan_duration_seconds": 0.25,
            "prop_arbitrage_candidates": [],
            "error_message": "odds api failed",
        },
        db_path=str(db_path),
        sport="basketball_nba",
        discovered_at="2026-06-05T10:00:00+00:00",
        finished_at="2026-06-05T10:00:01+00:00",
        error_message="odds api failed",
    )

    health = prop_watch_health(str(db_path), now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc))
    assert health["last_scan_time"] == "2026-06-05T10:00:00+00:00"
    assert health["last_scan_finished_at"] == "2026-06-05T10:00:01+00:00"
    assert health["last_scan_duration"] == 0.25
    assert health["opportunities_found_last_scan"] == 0
    assert health["error_message"] == "odds api failed"
