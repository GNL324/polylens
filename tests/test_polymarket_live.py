from __future__ import annotations

import polymarket_live_order_audit as audit
from src.adapters import polymarket_live as poly


class FakeAdapter:
    post_called = False

    def credentials_present(self):
        return {
            "ok": True,
            "credentials_detected_not_printed": True,
            "funder_detected_not_printed": True,
            "signature_type": 3,
        }

    def get_balance_allowance(self):
        return {"ok": True, "status": "ok", "raw": {"balance": "2"}}

    def order_args(self, *, token_id, price, size, side):
        return {"token_id": token_id, "price": price, "size": size, "side": side}

    def dry_run_order(self, **kwargs):
        return {"ok": True, "status": "signed", "order_args": kwargs, "order_endpoint_called": False, "sent": False}

    def post_order(self, *_args, **_kwargs):
        self.__class__.post_called = True
        return {"ok": True, "sent": True, "order_endpoint_called": True}


def _discovery(price=0.42, size=1.5, *, slug="btc-up-or-down-test", not_short_crypto=False, purpose=None):
    return {
        "ok": True,
        "market": {
            "slug": slug,
            "conditionId": "0xcondition",
            "minimum_tick_size": "0.01",
            "negRisk": False,
        },
        "outcome": {"name": "YES", "token_id": "123"},
        "orderbook": {
            "market": "0xcondition",
            "asset_id": "123",
            "timestamp": "9999999999999",
            "bids": [{"price": "0.40", "size": "2"}],
            "asks": [{"price": str(price), "size": str(size)}],
            "tick_size": "0.01",
            "neg_risk": False,
        },
        "best_ask": {"price": price, "size": size, "raw": {"price": str(price), "size": str(size)}},
        "candidates_checked": [{"asset": "BTC", "window_minutes": 5, "market_slug": slug, "token_id": "123"}],
        "clob_book_404_token_ids": [],
        "not_short_crypto": not_short_crypto,
        "purpose": purpose,
    }


def _short_crypto_miss():
    return {
        "ok": False,
        "reason": "no_short_crypto_clob_book_available",
        "candidates_checked": [
            {
                "asset": "BTC",
                "window_minutes": 5,
                "market_slug": "btc-5m-up",
                "token_id": "404-token",
                "book_status": 404,
                "book_reason": "clob_book_not_found",
            }
        ],
        "clob_book_404_token_ids": ["404-token"],
        "rejected": [],
        "not_short_crypto": False,
    }


def test_polymarket_credentials_missing_not_ready(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
    monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
    creds = poly.PolymarketLiveAdapter().credentials_present()
    assert creds["ok"] is False
    assert creds["credentials_detected_not_printed"] is False


def test_polymarket_live_send_blocked_by_default(monkeypatch):
    monkeypatch.delenv("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED", raising=False)
    res = poly.PolymarketLiveAdapter().post_order({"signed": "order"})
    assert res["status"] == "blocked"
    assert res["reason"] == "polymarket_live_sends_disabled"
    assert res["order_endpoint_called"] is False


def test_polymarket_first_live_run_id_required(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery())
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert "missing_first_live_test_run_id" in res["failed_gates"]


def test_polymarket_duplicate_run_id_blocked(monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery())
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    db_path = tmp_path / "p.db"
    key = poly.first_live_duplicate_key(market_slug="btc-up-or-down-test", token_id="123", run_id="test-001")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE polymarket_first_live_keys (dedupe_key TEXT PRIMARY KEY, created_at REAL NOT NULL)")
        conn.execute("INSERT INTO polymarket_first_live_keys VALUES (?, 1)", (key,))
    res = audit.build_audit(db_path=str(db_path))
    assert res["duplicate_status"]["duplicate"] is True
    assert "duplicate_trade_key" in res["failed_gates"]


def test_polymarket_first_live_max_exposure_lte_one(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery(price=0.99, size=1.0))
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert res["max_exposure"] == 0.99
    assert "first_live_test_max_exposure_gt_1" not in res["failed_gates"]


def test_polymarket_no_executable_ask_no_send(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    payload = _discovery()
    payload["best_ask"] = None
    payload["orderbook"]["asks"] = []
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: payload)
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert "no_executable_ask" in res["failed_gates"]
    assert res["sent"] is False
    assert res["order_endpoint_called"] is False


def test_polymarket_dry_run_does_not_call_post_order(monkeypatch, tmp_path):
    FakeAdapter.post_called = False
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery())
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert FakeAdapter.post_called is False
    assert res["order_endpoint_called"] is False


def test_polymarket_post_order_only_when_enabled(monkeypatch):
    class FakeClient:
        def post_order(self, signed, order_type):
            return {"signed": signed, "order_type": str(order_type)}

    class FakeOrderType:
        FOK = "FOK"

    monkeypatch.setenv("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED", "true")
    adapter = poly.PolymarketLiveAdapter()
    monkeypatch.setattr(adapter, "build_client", lambda: FakeClient())
    monkeypatch.setattr(poly, "_load_sdk", lambda: ("fake", None, None, None, FakeOrderType, None))
    for name in ("POLYLENS_FIRST_LIVE_TEST", "POLYLENS_LIVE_TRADING", "POLYLENS_AUTONOMOUS_CRYPTO", "POLYLENS_CONFIRM_RISK_ACK"):
        monkeypatch.setenv(name, "true")
    gate_context = {
        "run_id": "test-001",
        "max_exposure": 0.5,
        "orders_count": 1,
        "duplicate": False,
        "fresh_orderbook": True,
        "executable_ask": True,
        "balance_allowance_ok": True,
    }
    res = adapter.post_order({"signed": "order"}, "FOK", gate_context=gate_context)
    assert res["sent"] is True
    assert res["order_endpoint_called"] is True


def test_polymarket_post_order_enabled_but_missing_gates_does_not_call(monkeypatch):
    class FakeClient:
        called = False

        def post_order(self, signed, order_type):
            self.__class__.called = True
            return {}

    monkeypatch.setenv("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED", "true")
    adapter = poly.PolymarketLiveAdapter()
    monkeypatch.setattr(adapter, "build_client", lambda: FakeClient())
    res = adapter.post_order({"signed": "order"}, "FOK", gate_context={})
    assert res["status"] == "blocked"
    assert res["order_endpoint_called"] is False
    assert FakeClient.called is False


def test_polymarket_post_order_blocked_for_non_short_crypto_fallback(monkeypatch):
    monkeypatch.setenv("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED", "true")
    for name in ("POLYLENS_FIRST_LIVE_TEST", "POLYLENS_LIVE_TRADING", "POLYLENS_AUTONOMOUS_CRYPTO", "POLYLENS_CONFIRM_RISK_ACK"):
        monkeypatch.setenv(name, "true")
    gate_context = {
        "run_id": "test-001",
        "max_exposure": 0.5,
        "orders_count": 1,
        "duplicate": False,
        "fresh_orderbook": True,
        "executable_ask": True,
        "balance_allowance_ok": True,
        "not_short_crypto": True,
    }
    res = poly.PolymarketLiveAdapter().post_order({"signed": "order"}, "FOK", gate_context=gate_context)
    assert res["status"] == "blocked"
    assert res["order_endpoint_called"] is False
    assert "not_short_crypto_market" in res["failed_gates"]


def test_polymarket_funder_signature_type_surfaced_no_secrets(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "private-secret")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key-secret")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "passphrase-secret")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xfunder")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "3")
    creds = poly.PolymarketLiveAdapter().credentials_present()
    assert creds["ok"] is True
    assert creds["funder_detected_not_printed"] is True
    assert creds["signature_type"] == 3
    assert "private-secret" not in str(creds)
    assert "api-secret-secret" not in str(creds)


def test_short_crypto_mode_does_not_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())

    def _short_only():
        return _short_crypto_miss()

    monkeypatch.setattr(audit, "discover_short_crypto_market", _short_only)
    monkeypatch.setattr(
        audit,
        "discover_clob_connectivity_market",
        lambda *_args, **_kwargs: _discovery(slug="will-bitcoin-hit-150k-by-june-30-2026", not_short_crypto=True, purpose="clob_connectivity_only"),
    )
    res = audit.build_audit(db_path=str(tmp_path / "p.db"), mode="short_crypto")
    assert res["status"] == "not_ready"
    assert res["reason"] == "no_short_crypto_clob_book_available"
    assert res["clob_book_404_token_ids"] == ["404-token"]
    assert res.get("market_slug") is None


def test_clob_connectivity_mode_can_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    fallback = _discovery(
        slug="will-bitcoin-hit-150k-by-june-30-2026",
        not_short_crypto=True,
        purpose="clob_connectivity_only",
    )
    fallback["candidates_checked"] = _short_crypto_miss()["candidates_checked"]
    fallback["clob_book_404_token_ids"] = ["404-token"]
    monkeypatch.setattr(audit, "discover_clob_connectivity_market", lambda *_args, **_kwargs: fallback)
    res = audit.build_audit(db_path=str(tmp_path / "p.db"), mode="clob_connectivity")
    assert res["market_slug"] == "will-bitcoin-hit-150k-by-june-30-2026"
    assert res["not_short_crypto"] is True
    assert res["purpose"] == "clob_connectivity_only"
    assert res["live_send_allowed"] is False
    assert "not_short_crypto_market" in res["failed_gates"]


def test_readiness_not_ready_if_only_generic_clob_connectivity(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())

    def _short_only():
        return _short_crypto_miss()

    fallback = _discovery(
        slug="will-bitcoin-hit-150k-by-june-30-2026",
        not_short_crypto=True,
        purpose="clob_connectivity_only",
    )
    fallback["candidates_checked"] = _short_crypto_miss()["candidates_checked"]
    fallback["clob_book_404_token_ids"] = ["404-token"]

    monkeypatch.setattr(audit, "discover_short_crypto_market", _short_only)
    monkeypatch.setattr(audit, "discover_clob_connectivity_market", lambda *_args, **_kwargs: fallback)

    report = audit.live_readiness_report()
    assert report["status"] == "not_ready"
    short_check = next(item for item in report["checks"] if item["name"] == "polymarket_short_crypto_clob_book")
    clob_check = next(item for item in report["checks"] if item["name"] == "polymarket_clob_connectivity")
    assert short_check["ok"] is False
    assert short_check["reason"] == "no_short_crypto_clob_book_available"
    assert clob_check["ok"] is True
    assert clob_check["meta"]["not_short_crypto"] is True
    assert clob_check["meta"]["purpose"] == "clob_connectivity_only"


def test_no_live_send_from_non_short_crypto_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    fallback = _discovery(
        slug="will-bitcoin-hit-150k-by-june-30-2026",
        not_short_crypto=True,
        purpose="clob_connectivity_only",
    )
    monkeypatch.setattr(audit, "discover_clob_connectivity_market", lambda *_args, **_kwargs: fallback)
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    for name in (
        "POLYLENS_FIRST_LIVE_TEST",
        "POLYLENS_LIVE_TRADING",
        "POLYLENS_AUTONOMOUS_CRYPTO",
        "POLYLENS_CONFIRM_RISK_ACK",
        "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED",
    ):
        monkeypatch.setenv(name, "true")
    res = audit.build_audit(db_path=str(tmp_path / "p.db"), mode="clob_connectivity")
    assert res["live_send_allowed"] is False
    assert res["sent"] is False
    assert res["order_endpoint_called"] is False
    assert "not_short_crypto_market" in res["failed_gates"]
