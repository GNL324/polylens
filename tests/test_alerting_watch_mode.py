from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.alerts.notifier import ConsoleNotifier, MissingWebhookURLError, WebhookNotifier, build_alert_payload
from src.analysis.watch_mode import DuplicateSuppressor, watch_live_arbitrage


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b""


class RecordingNotifier:
    def __init__(self):
        self.payloads = []

    def notify(self, payload):
        self.payloads.append(payload)
        return {"sent": True}


def candidate(edge=0.06, score=0.82):
    return {
        "venue_pair": "polymarket_sportsbook",
        "polymarket_id": "pm-lakers",
        "polymarket_title": "Will the Lakers beat the Celtics?",
        "sportsbook_event_id": "evt1",
        "sportsbook": "Book",
        "sportsbook_team": "Los Angeles Lakers",
        "estimated_edge": edge,
        "execution_score": score,
        "score_reason": "edge available; confidence=0.90",
        "polymarket_implied_yes_price": 0.45,
        "sportsbook_implied_probability": 0.6,
        "commence_time": "2026-06-10T00:00:00Z",
    }


def scan_result(candidates=None, filter_reasons=None):
    candidates = candidates if candidates is not None else [candidate()]
    return {
        "top_candidates": candidates,
        "candidates_after_filtering": len(candidates),
        "filter_reasons": filter_reasons or {},
    }


def test_console_notifier(capsys):
    payload = build_alert_payload(candidate(), timestamp=datetime(2026, 6, 2, tzinfo=timezone.utc))
    result = ConsoleNotifier().notify(payload)
    assert result["sent"] is True
    assert "Polylens opportunity" in capsys.readouterr().out


def test_webhook_notifier_with_mocked_request(monkeypatch):
    monkeypatch.setenv("POLYLENS_WEBHOOK_URL", "https://example.com/webhook")
    payload = build_alert_payload(candidate(), timestamp=datetime(2026, 6, 2, tzinfo=timezone.utc))
    with patch("src.alerts.notifier.urlopen", return_value=FakeResponse()) as mocked:
        result = WebhookNotifier().notify(payload)
    assert result["channel"] == "webhook"
    assert mocked.called


def test_missing_webhook_url_error(monkeypatch):
    monkeypatch.delenv("POLYLENS_WEBHOOK_URL", raising=False)
    with pytest.raises(MissingWebhookURLError, match="POLYLENS_WEBHOOK_URL"):
        WebhookNotifier()


def test_duplicate_suppression():
    suppressor = DuplicateSuppressor(bucket_seconds=900)
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert suppressor.should_alert(candidate(), timestamp=now) is True
    assert suppressor.should_alert(candidate(), timestamp=now) is False
    later = datetime(2026, 6, 2, 12, 16, tzinfo=timezone.utc)
    assert suppressor.should_alert(candidate(), timestamp=later) is True


@patch("src.analysis.watch_mode.run_live_scan")
def test_watch_mode_once(mock_scan):
    notifier = RecordingNotifier()
    mock_scan.return_value = scan_result()
    result = watch_live_arbitrage(notifier, once=True, min_edge=0.02, min_score=0.5)
    assert result["iterations"] == 1
    assert result["alerts_sent"] == 1
    assert len(notifier.payloads) == 1


@patch("src.analysis.watch_mode.run_live_scan")
def test_watch_mode_filters(mock_scan):
    notifier = RecordingNotifier()
    mock_scan.return_value = scan_result(candidates=[], filter_reasons={"below min edge": 2})
    result = watch_live_arbitrage(notifier, once=True, min_edge=0.1, min_score=0.7)
    assert result["alerts_sent"] == 0
    assert result["scan"]["filter_reasons"]["below min edge"] == 2


@patch("src.cli.watch_live_arbitrage")
def test_watch_live_arb_cli_smoke(mock_watch, capsys):
    from src.cli import watch_live_arb

    mock_watch.return_value = {"iterations": 1, "alerts_sent": 0, "duplicates_suppressed": 0, "scan": {"candidates_after_filtering": 0, "filter_reasons": {}}}
    result = watch_live_arb(interval_seconds=1, min_edge=0.02, min_score=0.5, once=True)
    out = capsys.readouterr().out
    assert "Polylens Live Arbitrage Watch" in out
    assert result["iterations"] == 1
