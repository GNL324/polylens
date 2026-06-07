from __future__ import annotations

import json
from datetime import datetime, timezone

from src.adapters import polymarket_live as poly


def test_resolve_gamma_event_slug_mocked(monkeypatch):
    slug = "btc-updown-5m-1780859700"
    market = {
        "id": "2456539",
        "slug": slug,
        "conditionId": "0xabc",
        "enableOrderBook": True,
        "acceptingOrders": True,
        "active": True,
        "closed": False,
        "archived": False,
        "endDate": "2099-01-01T00:05:00Z",
        "clobTokenIds": '["111", "222"]',
        "outcomes": '["Up", "Down"]',
    }

    def fake_get(url):
        if "markets?" in url and f"slug={slug}" in url:
            return [market]
        if "events?" in url and f"slug={slug}" in url:
            return [{"id": "566714", "slug": slug, "seriesSlug": "btc-up-or-down-5m", "markets": [market]}]
        return []

    monkeypatch.setattr(poly, "_get_json", fake_get)
    monkeypatch.setattr(poly, "fetch_gamma_series_by_slug", lambda _s: {"slug": "btc-up-or-down-5m", "id": "10684", "events": [market]})
    monkeypatch.setattr(
        poly,
        "probe_clob_token_tradable",
        lambda token_id, host=None: {
            "token_id": token_id,
            "book": {"ok": True, "status_code": 200, "executable_book": True},
            "midpoint": {"ok": True, "status_code": 200},
            "tick_size": {"ok": True, "status_code": 200},
        },
    )
    res = poly.audit_polymarket_event_slug(slug)
    assert res["status"] == "ok"
    assert res["resolved"]["ids"]["market_id"] == "2456539"
    assert res["resolved"]["has_executable_book"] is True
    json.dumps(res)


def test_broad_scanner_does_not_discard_active_website_slug(monkeypatch):
    slug = "btc-updown-5m-1780859700"
    market = {
        "slug": slug,
        "question": "Bitcoin Up or Down - 5m",
        "enableOrderBook": True,
        "acceptingOrders": True,
        "active": True,
        "closed": False,
        "archived": False,
        "endDate": "2099-01-01T00:05:00Z",
        "clobTokenIds": '["111"]',
        "outcomes": '["Up", "Down"]',
    }
    monkeypatch.setattr(poly, "gamma_market_by_slug", lambda s: market if s == slug else None)
    monkeypatch.setattr(poly, "collect_gamma_tradable_crypto_candidates", lambda **kwargs: [])
    monkeypatch.setattr(poly, "_gamma_markets", lambda **kwargs: [])
    monkeypatch.setattr(
        poly,
        "series_live_event_markets",
        lambda series_slug: [market] if series_slug == "btc-up-or-down-5m" else [],
    )
    miss = poly.explain_broad_scanner_miss(slug, market)
    assert "expired_or_inactive_filter" not in miss["reasons"]
    assert "enable_order_book_false" not in miss["reasons"]


def test_stale_gamma_records_ignored_in_series_sort():
    now = datetime(2026, 6, 7, 19, 0, tzinfo=timezone.utc)
    stale = {"slug": "btc-updown-5m-old", "endDate": "2025-12-19T16:40:00Z", "enableOrderBook": False}
    live = {"slug": "btc-updown-5m-live", "endDate": "2026-06-07T19:10:00Z", "enableOrderBook": True}
    stale_secs = poly._market_seconds_to_close(stale, now=now)
    live_secs = poly._market_seconds_to_close(live, now=now)
    assert stale_secs is not None and stale_secs < 0
    assert live_secs is not None and live_secs > 0
    assert not poly.gamma_market_is_active_unexpired(stale, now=now)
    assert poly.gamma_market_is_active_unexpired(live, now=now)


def test_discover_short_crypto_uses_resolved_clob_token_ids(monkeypatch):
    slug = "btc-updown-5m-live"
    market = {
        "slug": slug,
        "question": "Bitcoin Up or Down - 5m",
        "conditionId": "0xabc",
        "enableOrderBook": True,
        "acceptingOrders": True,
        "active": True,
        "closed": False,
        "endDate": "2099-01-01T00:05:00Z",
        "clobTokenIds": '["111", "222"]',
        "outcomes": '["Up", "Down"]',
    }

    monkeypatch.setattr(poly, "series_live_event_markets", lambda series_slug: [market])
    monkeypatch.setattr(poly, "_gamma_markets", lambda **kwargs: [])
    monkeypatch.setattr(
        poly,
        "_probe_orderbook_with_variants",
        lambda token_id, host=None: {
            "ok": True,
            "book": {"asks": [{"price": 0.45, "size": 10}]},
            "status_code": 200,
            "attempts": [{"token_id_tried": str(token_id), "ok": True, "book_status": 200}],
        },
    )
    res = poly.discover_short_crypto_market(assets=("BTC",), windows=(5,))
    assert res["ok"] is True
    assert res["outcome"]["token_id"] == "111"


def test_series_slug_map_includes_btc_5m():
    assert poly.SHORT_CRYPTO_SERIES_BY_ASSET["BTC"][5] == "btc-up-or-down-5m"
