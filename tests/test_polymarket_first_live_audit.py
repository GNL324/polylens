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


def _discovery(price=0.42, size=1.5, *, slug="btc-updown-5m-test"):
    return {
        "ok": True,
        "market": {
            "slug": slug,
            "conditionId": "0xcondition",
            "minimum_tick_size": "0.01",
            "negRisk": False,
        },
        "outcome": {"name": "Up", "token_id": "123"},
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
        "not_short_crypto": False,
    }


def _enable_first_live(monkeypatch):
    monkeypatch.setenv("POLYLENS_POLYMARKET_FIRST_LIVE_TEST", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "pm-test-001")
    for name in (
        "POLYLENS_LIVE_TRADING",
        "POLYLENS_AUTONOMOUS_CRYPTO",
        "POLYLENS_CONFIRM_RISK_ACK",
        "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED",
    ):
        monkeypatch.setenv(name, "true")


def test_polymarket_first_live_audit_kalshi_parity_fields(monkeypatch, tmp_path):
    FakeAdapter.post_called = False
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery())
    _enable_first_live(monkeypatch)
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert res["count"] == 1
    assert res["intended_size"] == 1.0
    assert res["exact_order_payload"]["size"] == 1.0
    assert res["economic_exposure"]["max_exposure_dollars"] == res["max_exposure"]
    assert res["selected_candidate"]["market_slug"] == "btc-updown-5m-test"
    assert res["selected_candidate"]["token_id"] == "123"
    assert res["selected_ask_price"] == 0.42
    assert "validations" in res
    assert res["validations"]["polymarket_first_live_test_mode"]["ok"] is True
    assert res["validations"]["would_pass_live_gates"]["ok"] is True
    assert res["post_order_called"] is False
    assert res["sent"] is False
    assert res["order_endpoint_called"] is False


def test_polymarket_first_live_test_env_required(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery())
    monkeypatch.delenv("POLYLENS_POLYMARKET_FIRST_LIVE_TEST", raising=False)
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "pm-test-001")
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert "missing_live_gate_polylens_polymarket_first_live_test" in res["failed_gates"]


def test_polymarket_first_live_max_exposure_lte_one_dollar(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery(price=0.99, size=2.0))
    _enable_first_live(monkeypatch)
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert res["max_exposure"] == 0.99
    assert res["economic_exposure"]["count"] == 1
    assert "first_live_test_max_exposure_gt_1_dollar" not in res["failed_gates"]


def test_polymarket_first_live_rejects_exposure_gt_one(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery(price=1.5, size=2.0))
    _enable_first_live(monkeypatch)
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert "price_must_be_between_0_and_1" in res["validations"]["payload_valid"]["problems"]


def test_polymarket_dry_run_never_calls_post_order(monkeypatch, tmp_path):
    FakeAdapter.post_called = False
    monkeypatch.setattr(audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(audit, "discover_short_crypto_market", lambda *_args, **_kwargs: _discovery())
    _enable_first_live(monkeypatch)
    res = audit.build_audit(db_path=str(tmp_path / "p.db"))
    assert FakeAdapter.post_called is False
    assert res["post_order_called"] is False


def test_polymarket_post_order_gates_use_polymarket_first_live_env(monkeypatch):
    monkeypatch.setenv("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED", "true")
    for name in (
        "POLYLENS_POLYMARKET_FIRST_LIVE_TEST",
        "POLYLENS_LIVE_TRADING",
        "POLYLENS_AUTONOMOUS_CRYPTO",
        "POLYLENS_CONFIRM_RISK_ACK",
    ):
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
    gate = poly.validate_first_live_send_gates(gate_context)
    assert gate["ok"] is True
