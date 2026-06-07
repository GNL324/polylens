from __future__ import annotations

import json
from datetime import datetime, timezone

from src.adapters import polymarket_live as poly


def _market(**overrides):
    base = {
        "slug": "will-ethereum-hit-10k",
        "question": "Will Ethereum hit $10k in 2026?",
        "active": True,
        "closed": False,
        "archived": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "clobTokenIds": '["111", "222"]',
        "outcomes": '["Yes", "No"]',
        "endDate": "2099-01-01T00:00:00Z",
        "liquidityClob": 1000,
    }
    base.update(overrides)
    return base


def test_gamma_market_is_active_unexpired_filters_expired():
    ok = poly.gamma_market_is_active_unexpired(
        _market(endDate="2020-01-01T00:00:00Z"),
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert ok is False


def test_build_tradable_crypto_market_record_detects_enable_order_book_true():
    record = poly.build_tradable_crypto_market_record(_market(enableOrderBook=True))
    assert record["enable_order_book"] is True


def test_build_tradable_crypto_market_record_detects_executable_book(monkeypatch):
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
    record = poly.build_tradable_crypto_market_record(_market())
    assert record["has_executable_book"] is True
    assert record["tradable_clob"] is True


def test_market_looks_crypto_related_does_not_require_updown_slug():
    market = _market(slug="will-ethereum-hit-10k", question="Will Ethereum hit $10k?")
    assert poly.market_looks_crypto_related(market) is True
    assert "updown" not in market["slug"]


def test_guess_market_window_from_title_and_end_date():
    market = _market(
        slug="some-market",
        question="Bitcoin Up or Down - 5 minute window",
        endDate="2099-01-01T00:05:00Z",
    )
    seconds = poly._market_seconds_to_close(
        market,
        now=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert poly.guess_market_window(market, seconds_to_close=seconds) == "5m"
    assert poly.is_short_crypto_market_candidate(
        market,
        asset=poly.guess_tradable_crypto_asset(market),
        window_guess="5m",
    ) is True


def test_probe_clob_token_tradable_does_not_require_price_endpoint(monkeypatch):
    calls = []

    def fake_endpoint(path, token_id, host=None):
        calls.append(path)
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(poly, "_probe_clob_endpoint", fake_endpoint)
    monkeypatch.setattr(
        poly,
        "_probe_orderbook",
        lambda token_id, host=None: {
            "ok": True,
            "book": {"asks": [{"price": 0.4, "size": 10}]},
            "status_code": 200,
        },
    )
    res = poly.probe_clob_token_tradable("111")
    assert res["book"]["executable_book"] is True
    assert "price" not in calls


def test_build_tradable_crypto_discovery_summary_shape(monkeypatch):
    sample_market = _market(slug="will-bitcoin-hit-150k", question="Will Bitcoin hit $150k?")
    monkeypatch.setattr(poly, "collect_gamma_tradable_crypto_candidates", lambda **_kwargs: [sample_market])
    monkeypatch.setattr(
        poly,
        "build_tradable_crypto_market_record",
        lambda market, now=None: {
            "slug": market["slug"],
            "tradable_clob": True,
            "has_executable_book": True,
            "enable_order_book": True,
            "is_short_crypto_candidate": False,
            "window_guess": "long_dated",
            "asset": "BTC",
            "liquidity_clob": 500,
            "volume_24hr_clob": 100,
        },
    )
    payload = poly.build_tradable_crypto_discovery()
    assert payload["summary"]["total_gamma_candidates"] == 1
    assert payload["summary"]["executable_books"] == 1
    json.dumps(payload)

def test_market_looks_crypto_related_rejects_eth_substring_false_positives():
    market = {
        "question": "Will Pete Hegseth win the 2028 US Presidential Election?",
        "slug": "will-pete-hegseth-win-the-2028-us-presidential-election",
    }
    assert poly.market_looks_crypto_related(market) is False
