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


from src.notifications.dedupe import fingerprint, should_alert


def _prop_opportunity(**overrides):
    opportunity = {
        "sport": "basketball_nba",
        "event_id": "evt-123",
        "market_type": "player_points",
        "player": "Mitchell Robinson",
        "line": 2.5,
        "over_book": "BetA",
        "under_book": "BetB",
        "over_odds": -110,
        "under_odds": -115,
        "guaranteed_roi": 0.019056,
        "guaranteed_profit_amount": 1.5,
    }
    opportunity.update(overrides)
    return opportunity


def test_prop_alert_cooldown():
    opportunity = _prop_opportunity()
    allowed, reason = should_alert(opportunity)
    assert allowed is True

    allowed, reason = should_alert(opportunity)
    assert allowed is False
    assert reason == "cooldown"


def test_prop_alert_realert_roi():
    opportunity = _prop_opportunity(guaranteed_roi=0.019056)
    allowed, reason = should_alert(opportunity)
    assert allowed is True

    improved = _prop_opportunity(guaranteed_roi=0.025056)
    allowed, reason = should_alert(improved)
    assert allowed is True


def test_prop_alert_realert_profit():
    base = _prop_opportunity(guaranteed_profit_amount=4.0)
    allowed, reason = should_alert(base)
    assert allowed is True

    improved = _prop_opportunity(guaranteed_profit_amount=10.5)
    allowed, reason = should_alert(improved)
    assert allowed is True


def test_prop_alert_timestamp_noop():
    base = _prop_opportunity()
    base["timestamp"] = "2026-01-01T00:00:00Z"
    allowed, reason = should_alert(base, now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    assert allowed is True

    changed_timestamp = dict(base)
    changed_timestamp["timestamp"] = "2026-01-01T00:01:00Z"
    allowed, reason = should_alert(changed_timestamp, now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc))
    assert allowed is False
    assert reason == "cooldown"


def test_prop_alert_different_book_pair():
    opportunity_a = _prop_opportunity(over_book="BookA", under_book="BookB")
    allowed_a, _ = should_alert(opportunity_a)
    assert allowed_a is True

    opportunity_b = _prop_opportunity(over_book="BookC", under_book="BookB")
    allowed_b, _ = should_alert(opportunity_b)
    assert allowed_b is True


def test_prop_alert_different_line():
    opportunity = _prop_opportunity(line=2.5)
    allowed, _ = should_alert(opportunity)
    assert allowed is True

    changed_line = _prop_opportunity(line=3.5)
    allowed, _ = should_alert(changed_line)
    assert allowed is True


from src.notifications.dedupe import fingerprint, should_alert


def _prop_opportunity(**overrides):
    opportunity = {
        "sport": "basketball_nba",
        "event_id": "evt-123",
        "market_type": "player_points",
        "player": "Mitchell Robinson",
        "line": 2.5,
        "over_book": "BetA",
        "under_book": "BetB",
        "over_odds": -110,
        "under_odds": -115,
        "guaranteed_roi": 0.019056,
        "guaranteed_profit_amount": 1.5,
    }
    opportunity.update(overrides)
    return opportunity


def test_prop_alert_cooldown():
    opportunity = _prop_opportunity()
    allowed, reason = should_alert(opportunity)
    assert allowed is True

    allowed, reason = should_alert(opportunity)
    assert allowed is False
    assert reason == "cooldown"


def test_prop_alert_realert_roi():
    opportunity = _prop_opportunity(guaranteed_roi=0.019056)
    allowed, reason = should_alert(opportunity)
    assert allowed is True

    improved = _prop_opportunity(guaranteed_roi=0.025056)
    allowed, reason = should_alert(improved)
    assert allowed is True


def test_prop_alert_realert_profit():
    base = _prop_opportunity(guaranteed_profit_amount=4.0)
    allowed, reason = should_alert(base)
    assert allowed is True

    improved = _prop_opportunity(guaranteed_profit_amount=10.5)
    allowed, reason = should_alert(improved)
    assert allowed is True


def test_prop_alert_timestamp_noop():
    base = _prop_opportunity()
    base["timestamp"] = "2026-01-01T00:00:00Z"
    allowed, reason = should_alert(base, now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    assert allowed is True

    changed_timestamp = dict(base)
    changed_timestamp["timestamp"] = "2026-01-01T00:01:00Z"
    allowed, reason = should_alert(changed_timestamp, now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc))
    assert allowed is False
    assert reason == "cooldown"


def test_prop_alert_different_book_pair():
    opportunity_a = _prop_opportunity(over_book="BookA", under_book="BookB")
    allowed_a, _ = should_alert(opportunity_a)
    assert allowed_a is True

    opportunity_b = _prop_opportunity(over_book="BookC", under_book="BookB")
    allowed_b, _ = should_alert(opportunity_b)
    assert allowed_b is True


def test_prop_alert_different_line():
    opportunity = _prop_opportunity(line=2.5)
    allowed, _ = should_alert(opportunity)
    assert allowed is True

    changed_line = _prop_opportunity(line=3.5)
    allowed, _ = should_alert(changed_line)
    assert allowed is True


# ===== Crypto-specific fingerprinting tests =====

def _crypto_opportunity(**overrides):
    """Create a crypto opportunity for fingerprinting tests."""
    opportunity = {
        "asset": "BTC",
        "venue": "polymarket",
        "direction": "up",
        "window_minutes": 5,
        "market": "btc-updown-5m-12345",
        "condition_id": "cond-123",
        "ticker": "BTC-USD-5M-UP",
        "slug": "btc-updown-5m",
        "token_id": "token-123",
        "model_probability": 0.52,
        "edge": 0.01,
        "guaranteed_roi": 0.05,
        "guaranteed_profit_amount": 50.0,
    }
    opportunity.update(overrides)
    return opportunity


def test_crypto_fingerprint_repeated_opportunity_matches():
    """Same crypto opportunity should produce same fingerprint."""
    opp = _crypto_opportunity()
    fp1 = fingerprint(opp)
    fp2 = fingerprint(opp)
    assert fp1 == fp2


def test_crypto_fingerprint_different_window_differs():
    """BTC 5m UP should differ from BTC 15m UP."""
    opp_5m = _crypto_opportunity(window_minutes=5, market="btc-updown-5m-12345")
    opp_15m = _crypto_opportunity(window_minutes=15, market="btc-updown-15m-12345")
    fp_5m = fingerprint(opp_5m)
    fp_15m = fingerprint(opp_15m)
    assert fp_5m != fp_15m


def test_crypto_fingerprint_different_direction_differs():
    """BTC 5m UP should differ from BTC 5m DOWN."""
    opp_up = _crypto_opportunity(direction="up", market="btc-updown-5m-12345")
    opp_down = _crypto_opportunity(direction="down", market="btc-downup-5m-12345")
    fp_up = fingerprint(opp_up)
    fp_down = fingerprint(opp_down)
    assert fp_up != fp_down


def test_crypto_fingerprint_different_market_differs():
    """Different markets should generate different fingerprints."""
    opp_a = _crypto_opportunity(market="btc-updown-5m-11111", ticker="BTC-5M-UP-1")
    opp_b = _crypto_opportunity(market="btc-updown-5m-22222", ticker="BTC-5M-UP-2")
    fp_a = fingerprint(opp_a)
    fp_b = fingerprint(opp_b)
    assert fp_a != fp_b


def test_crypto_fingerprint_different_asset_differs():
    """BTC vs ETH should differ."""
    opp_btc = _crypto_opportunity(asset="BTC", market="btc-updown-5m-123")
    opp_eth = _crypto_opportunity(asset="ETH", market="eth-updown-5m-123")
    fp_btc = fingerprint(opp_btc)
    fp_eth = fingerprint(opp_eth)
    assert fp_btc != fp_eth


def test_crypto_fingerprint_different_venue_differs():
    """Polymarket vs Kalshi should differ."""
    opp_poly = _crypto_opportunity(venue="polymarket", market="btc-updown-5m-123")
    opp_kalshi = _crypto_opportunity(venue="kalshi", ticker="BTC-USD-5M-UP")
    fp_poly = fingerprint(opp_poly)
    fp_kalshi = fingerprint(opp_kalshi)
    assert fp_poly != fp_kalshi


def test_crypto_fingerprint_falls_back_to_tokens_and_slugs():
    """When market is missing, should use token_id or slug."""
    opp1 = _crypto_opportunity(market="", condition_id="", ticker="", slug="btc-5m-a", token_id="token-abc")
    opp2 = _crypto_opportunity(market="", condition_id="", ticker="", slug="btc-5m-b", token_id="token-xyz")
    fp1 = fingerprint(opp1)
    fp2 = fingerprint(opp2)
    # Different slugs should produce different fingerprints
    assert fp1 != fp2
