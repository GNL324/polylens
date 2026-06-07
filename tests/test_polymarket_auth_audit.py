from __future__ import annotations

import polymarket_auth_audit as auth_audit
from src.adapters import polymarket_live as poly


class FakeAdapter:
    post_called = False
    init_called = False

    def credentials_present(self):
        return {
            "ok": True,
            "private_key_present": True,
            "api_key_present": True,
            "api_secret_present": True,
            "api_passphrase_present": True,
            "credentials_detected_not_printed": True,
            "funder_detected_not_printed": True,
            "signature_type": 3,
            "chain_id": 137,
            "clob_host": "https://clob.polymarket.com",
        }

    def try_init_client(self):
        self.__class__.init_called = True
        return {"ok": True, "client_type": "FakeClobClient", "attempted": True}

    def get_balance_allowance(self):
        return {"ok": True, "status": "ok", "raw": {"balance": "1"}, "attempted": True}

    def dry_run_order(self, **kwargs):
        return {"ok": True, "status": "signed", "order_endpoint_called": False, "sent": False, "attempted": True}

    def post_order(self, *_args, **_kwargs):
        self.__class__.post_called = True
        return {"ok": True, "sent": True}


def test_auth_audit_missing_credentials_not_ready(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
    monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
    res = auth_audit.build_auth_audit()
    assert res["status"] == "not_ready"
    assert "polymarket_credentials_missing" in res["failed_checks"]
    assert res["clob_client_init"]["attempted"] is False
    assert res["post_order_called"] is False
    assert res["sent"] is False


def test_auth_audit_secrets_not_printed(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "super-secret-private-key")
    monkeypatch.setenv("POLYMARKET_API_KEY", "super-secret-api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "super-secret-api-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "super-secret-passphrase")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0xdeadbeef")
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    blob = str(res)
    assert "super-secret-private-key" not in blob
    assert "super-secret-api-secret" not in blob
    assert "super-secret-passphrase" not in blob
    assert res["credentials"]["POLYMARKET_PRIVATE_KEY_present"] is True
    assert res["credentials"]["POLYMARKET_FUNDER_present"] is True


def test_auth_audit_client_init_only_when_credentials_present(monkeypatch):
    FakeAdapter.init_called = False
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    assert FakeAdapter.init_called is True
    assert res["clob_client_init"]["ok"] is True


def test_auth_audit_post_order_never_called(monkeypatch):
    FakeAdapter.post_called = False
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapter())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    assert FakeAdapter.post_called is False
    assert res["post_order_called"] is False
    assert res["sent"] is False


def test_short_crypto_clob_404_diagnostics_include_slug_token_outcome(monkeypatch):
    def fake_gamma(*_args, **_kwargs):
        return [
            {
                "slug": "btc-updown-5m-test",
                "conditionId": "0xabc",
                "clobTokenIds": ["404-token", "other-token"],
                "outcomes": ["Up", "Down"],
            }
        ]

    def fake_probe(token_id, host=None):
        return {
            "ok": False,
            "book": None,
            "status_code": 404,
            "token_id_used": None,
            "attempts": [
                {"token_id_tried": str(token_id), "book_status": 404, "ok": False, "reason": "clob_book_not_found", "error": "HTTP Error 404: Not Found"},
            ],
            "reason": "clob_book_not_found",
            "detail": "HTTP Error 404: Not Found",
            "error": "HTTP Error 404: Not Found",
        }

    monkeypatch.setattr(poly, "_gamma_markets", fake_gamma)
    monkeypatch.setattr(poly, "_probe_orderbook_with_variants", fake_probe)
    res = poly.discover_short_crypto_market(assets=("BTC",), windows=(5,))
    assert res["ok"] is False
    assert res["reason"] == "no_short_crypto_clob_book_available"
    assert res["diagnostics"]["all_5m_books_404"] is True
    sample = res["candidates_checked"][0]
    assert sample["slug"] == "btc-updown-5m-test"
    assert sample["condition_id"] == "0xabc"
    assert sample["clobTokenIds"] == ["404-token", "other-token"]
    assert sample["token_id"] == "404-token"
    assert sample["outcome"] == "Up"
    assert sample["book_status"] == 404
    assert sample["error"]


def test_token_id_probe_variants_raw_and_string():
    variants = poly._token_id_probe_variants(12345)
    assert variants == ["12345"]
    variants2 = poly._token_id_probe_variants("12345")
    assert variants2 == ["12345"]


def test_probe_sdk_reports_import_status():
    probe = poly.probe_sdk()
    assert "imports" in probe
    assert "py_clob_client_v2" in probe["imports"]
    assert "py_clob_client" in probe["imports"]
    assert probe["sdk_available"] is bool(probe.get("sdk_package"))


def test_auth_audit_sdk_available_when_probe_succeeds(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
    monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
    monkeypatch.setattr(
        auth_audit,
        "probe_sdk",
        lambda: {
            "sdk_available": True,
            "sdk_package": "py_clob_client_v2",
            "imports": {
                "py_clob_client_v2": {"ok": True, "error": None},
                "py_clob_client": {"ok": False, "error": "ModuleNotFoundError: No module named 'py_clob_client'"},
            },
        },
    )
    res = auth_audit.build_auth_audit()
    assert res["sdk_available"] is True
    assert res["sdk_package"] == "py_clob_client_v2"
    assert "polymarket_sdk_not_available" not in res["failed_checks"]


def test_auth_audit_sdk_not_available_when_probe_fails(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(
        auth_audit,
        "probe_sdk",
        lambda: {
            "sdk_available": False,
            "sdk_package": None,
            "imports": {
                "py_clob_client_v2": {"ok": False, "error": "ModuleNotFoundError: missing"},
                "py_clob_client": {"ok": False, "error": "ModuleNotFoundError: missing"},
            },
        },
    )
    res = auth_audit.build_auth_audit()
    assert res["sdk_available"] is False
    assert "polymarket_sdk_not_available" in res["failed_checks"]

import json
import types

import polymarket_auth_audit as auth_audit


class FakeSide:
    name = "BUY"
    __objclass__ = types.MappingProxyType({"BUY": "buy", "SELL": "sell"})


class FakeSignedOrder:
    signature = "super-secret-signature-value-should-not-appear"
    side = FakeSide()
    token_id = "13915689317269078219168496739008737517740566192006337297676041270492637394586"
    price = 0.01
    size = 1.0


class FakeAdapterWithSignedOrder(FakeAdapter):
    def dry_run_order(self, **kwargs):
        return {
            "ok": True,
            "status": "signed",
            "order_endpoint_called": False,
            "sent": False,
            "attempted": True,
            "signed_order": FakeSignedOrder(),
        }


def test_auth_audit_json_serializes_with_mocked_signed_order(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("POLYMARKET_API_KEY", "existing-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "existing-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "existing-passphrase")
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapterWithSignedOrder())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    encoded = json.dumps(res, indent=2, sort_keys=True)
    signed = res["dry_run_signing"]["signed_order"]
    assert signed["signed_order_created"] is True
    assert signed["order_type"] == "FakeSignedOrder"
    assert signed["has_signature"] is True
    assert signed["side"] == "BUY"
    assert signed["token_id_present"] is True
    assert signed["price_present"] is True
    assert signed["size_present"] is True
    assert "mappingproxy" not in encoded.lower()
    assert "__objclass__" not in encoded


def test_auth_audit_no_signature_value_printed(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("POLYMARKET_API_KEY", "existing-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "existing-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "existing-passphrase")
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapterWithSignedOrder())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    encoded = json.dumps(res)
    assert "super-secret-signature-value-should-not-appear" not in encoded
    assert '"signature"' not in encoded


def test_auth_audit_no_private_or_api_secret_printed(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "super-secret-private-key-value")
    monkeypatch.setenv("POLYMARKET_API_KEY", "super-secret-api-key-value")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "super-secret-api-secret-value")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "super-secret-passphrase-value")
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapterWithSignedOrder())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    encoded = json.dumps(res)
    assert "super-secret-private-key-value" not in encoded
    assert "super-secret-api-secret-value" not in encoded
    assert "super-secret-passphrase-value" not in encoded
import enum
import json
from types import SimpleNamespace

import polymarket_auth_audit as auth_audit
from src.adapters import polymarket_live as poly


class SideEnum(enum.IntEnum):
    BUY = 0
    SELL = 1


class FakeAdapterBalance401(FakeAdapter):
    def get_balance_allowance(self):
        return {
            "ok": False,
            "status": "not_ready",
            "reason": "balance_allowance_unauthorized",
            "auth_valid": False,
            "balance_allowance_attempts": [
                {"asset_type": "COLLATERAL", "ok": False, "status": "error", "status_code": 401, "error": {"error": "Unauthorized"}},
            ],
            "first_successful_asset_type": None,
            "balance_summary": None,
        }


class FakeAdapterBalance400(FakeAdapter):
    def get_balance_allowance(self):
        return {
            "ok": False,
            "status": "not_ready",
            "reason": "balance_allowance_request_malformed",
            "auth_valid": True,
            "balance_allowance_attempts": [
                {"asset_type": "none", "ok": False, "status": "error", "status_code": 400, "error": {"error": "Invalid asset type"}},
                {"asset_type": "usdc", "ok": False, "status": "error", "status_code": 400, "error": {"error": "Invalid asset type"}},
            ],
            "first_successful_asset_type": None,
            "balance_summary": None,
        }


def test_derive_auth_valid_401_means_auth_invalid():
    attempts = [{"status_code": 401, "ok": False}]
    assert poly.derive_auth_valid_from_attempts(attempts) is False


def test_derive_auth_valid_400_invalid_asset_type_means_auth_valid():
    attempts = [{"status_code": 400, "ok": False, "error": {"error": "Invalid asset type"}}]
    assert poly.derive_auth_valid_from_attempts(attempts) is True


def test_auth_audit_auth_valid_false_on_401(monkeypatch):
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapterBalance401())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    assert res["auth_valid"] is False
    assert res["balance_allowance"]["balance_allowance_attempts"][0]["status_code"] == 401


def test_auth_audit_auth_valid_true_on_400_invalid_asset_type(monkeypatch):
    monkeypatch.setattr(auth_audit, "PolymarketLiveAdapter", lambda: FakeAdapterBalance400())
    monkeypatch.setattr(auth_audit, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2", "imports": {}})
    res = auth_audit.build_auth_audit()
    assert res["auth_valid"] is True
    assert res["balance_allowance"]["reason"] == "balance_allowance_request_malformed"


def test_extract_side_string_int_enum_buy_sell():
    assert auth_audit._extract_side_string(SimpleNamespace(side=SideEnum.BUY)) == "BUY"
    assert auth_audit._extract_side_string(SimpleNamespace(side=SideEnum.SELL)) == "SELL"


def test_extract_side_string_does_not_leak_enum_internals():
    side_obj = SideEnum.BUY
    result = auth_audit._extract_side_string(SimpleNamespace(side=side_obj))
    encoded = json.dumps({"side": result})
    assert result == "BUY"
    assert "IntEnum" not in encoded
    assert "mappingproxy" not in encoded.lower()
    assert "__objclass__" not in encoded
