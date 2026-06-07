from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import polymarket_auth_audit as auth_audit
import polymarket_short_crypto_discovery_audit as discovery_audit
from src.adapters import polymarket_live as poly


def _market(**overrides):
    base = {
        "id": "1",
        "slug": "btc-updown-5m-test",
        "conditionId": "0xabc",
        "question": "Bitcoin Up or Down - 5m",
        "active": True,
        "closed": False,
        "archived": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "clobTokenIds": '["111", "222"]',
        "outcomes": '["Up", "Down"]',
        "endDate": "2099-01-01T00:05:00Z",
    }
    base.update(overrides)
    return base


def test_short_crypto_market_eligible_rejects_non_clob_enabled():
    ok, reason = poly._short_crypto_market_eligible(_market(enableOrderBook=False))
    assert ok is False
    assert reason == "gamma_market_not_clob_enabled"


def test_short_crypto_market_eligible_rejects_expired_window():
    ok, reason = poly._short_crypto_market_eligible(
        _market(endDate="2020-01-01T00:05:00Z"),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert ok is False
    assert reason == "market_window_expired"


def test_discover_candidate_token_ids_includes_clob_and_tokens_fields():
    market = _market(
        tokens=[
            {"token_id": "333", "outcome": "Up"},
            {"token_id": "444", "outcome": "Down"},
        ]
    )
    token_ids = poly.discover_candidate_token_ids(market)
    assert "111" in token_ids
    assert "333" in token_ids


def test_discover_short_crypto_skips_non_clob_enabled(monkeypatch):
    market = _market(enableOrderBook=False, endDate="2099-01-01T00:05:00Z")
    monkeypatch.setattr(poly, "_gamma_markets", lambda **_kwargs: [market])

    def _fail_probe(*_args, **_kwargs):
        raise AssertionError("probe should not run for non-clob-enabled market")

    monkeypatch.setattr(poly, "_probe_orderbook_with_variants", _fail_probe)
    res = poly.discover_short_crypto_market(assets=("BTC",), windows=(5,))
    assert res["ok"] is False
    assert any(item.get("reason") == "gamma_market_not_clob_enabled" for item in res.get("rejected", []))


def test_probe_clob_token_endpoints_reports_404_diagnostics(monkeypatch):
    monkeypatch.setattr(
        poly,
        "_probe_orderbook",
        lambda token_id, host=None: {
            "ok": False,
            "book": None,
            "status_code": 404,
            "reason": "clob_book_not_found",
            "detail": "HTTP Error 404: Not Found",
        },
    )
    monkeypatch.setattr(
        poly,
        "_probe_clob_endpoint",
        lambda path, token_id, host=None: {"ok": False, "status_code": 404, "error": "HTTP Error 404: Not Found"},
    )
    res = poly.probe_clob_token_endpoints("111")
    assert res["book"]["status_code"] == 404
    assert res["price"]["status_code"] == 404


def test_probe_clob_token_endpoints_does_not_treat_price_as_executable_book(monkeypatch):
    def fake_probe(path, token_id, host=None):
        if path in {"price", "midpoint", "tick-size"}:
            return {"ok": True, "status_code": 200, "data_present": True}
        return {"ok": False, "status_code": 404, "error": "not found"}

    monkeypatch.setattr(poly, "_probe_clob_endpoint", fake_probe)
    monkeypatch.setattr(
        poly,
        "_probe_orderbook",
        lambda token_id, host=None: {"ok": False, "book": None, "status_code": 404, "reason": "clob_book_not_found"},
    )
    res = poly.probe_clob_token_endpoints("111")
    assert res["price"]["ok"] is True
    assert res["midpoint"]["ok"] is True
    assert res["book"]["executable_book"] is False


def test_diagnose_gamma_market_not_clob_enabled():
    candidates = [{"enable_order_book": False, "executable_book": False, "token_probes": [{"book": {"status_code": 404}}]}]
    known = {"executable_book": True}
    assert poly.diagnose_short_crypto_clob_issue(candidates, known) == "gamma_market_not_clob_enabled"


def test_diagnose_clob_registration_delay():
    candidates = [
        {
            "enable_order_book": True,
            "executable_book": False,
            "seconds_to_close": 3600,
            "token_probes": [{"book": {"status_code": 404}}],
        }
    ]
    known = {"executable_book": True}
    assert poly.diagnose_short_crypto_clob_issue(candidates, known) == "clob_registration_delay"


def test_diagnose_token_field_bug_when_alternate_token_ids_present():
    candidates = [
        {
            "enable_order_book": True,
            "executable_book": False,
            "token_id_fields": {"tokens": [{"token_id": "999"}]},
            "token_probes": [{"book": {"status_code": 404}}],
        }
    ]
    known = {"executable_book": True}
    assert poly.diagnose_short_crypto_clob_issue(candidates, known) == "token_field_bug"


def test_extract_side_string_buy_sell_only():
    class Side(enum.IntEnum):
        BUY = 0
        SELL = 1

    assert auth_audit._extract_side_string(SimpleNamespace(side=Side.BUY)) == "BUY"
    assert auth_audit._extract_side_string(SimpleNamespace(side=Side.SELL)) == "SELL"
    encoded = json.dumps({"side": auth_audit._extract_side_string(SimpleNamespace(side=Side.BUY))})
    assert "IntEnum" not in encoded


def test_discovery_audit_json_serializes(monkeypatch):
    sample = {
        "status": "ok",
        "candidate_count": 1,
        "candidates": [{"slug": "btc-updown-5m-test", "executable_book": False}],
        "known_good_market": {"slug": "will-bitcoin-hit-150k-by-june-30-2026", "executable_book": True},
        "sample_comparison": {"field_diffs": {"enable_order_book": {"short_crypto": False, "known_good": True}}},
        "diagnosis": "gamma_market_not_clob_enabled",
        "notes": {},
    }
    monkeypatch.setattr(discovery_audit, "enumerate_short_crypto_discovery_audit", lambda: sample)
    payload = discovery_audit.enumerate_short_crypto_discovery_audit()
    json.dumps(payload)
