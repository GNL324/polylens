from __future__ import annotations

from types import SimpleNamespace

import polymarket_credentials_setup as setup


class FakeApiCreds:
    api_key = "derived-api-key-secret-value"
    api_secret = "derived-api-secret-secret-value"
    api_passphrase = "derived-passphrase-secret-value"


def test_credentials_setup_secrets_not_printed(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
    monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
    monkeypatch.setattr(setup, "derive_l2_api_credentials", lambda _cfg: FakeApiCreds())
    monkeypatch.setattr(setup, "probe_sdk", lambda: {"sdk_available": True, "sdk_package": "py_clob_client_v2"})
    res = setup.build_credentials_setup(write_env_file=False)
    blob = str(res)
    assert "derived-api-key-secret-value" not in blob
    assert "derived-api-secret-secret-value" not in blob
    assert "derived-passphrase-secret-value" not in blob
    assert res["l2"]["api_key_present"] is True


def test_generated_env_file_is_gitignored():
    import pathlib

    gitignore = (pathlib.Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".polymarket.env.generated" in gitignore


def test_missing_private_key_blocks_derivation(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    res = setup.build_credentials_setup(write_env_file=False)
    assert res["status"] == "not_ready"
    assert res["reason"] == "POLYMARKET_PRIVATE_KEY_missing"
    assert res["derived"] is False


def test_existing_l2_creds_skip_derivation(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "b" * 64)
    monkeypatch.setenv("POLYMARKET_API_KEY", "existing-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "existing-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "existing-passphrase")

    def _fail_derive(_cfg):
        raise AssertionError("derive should not be called")

    monkeypatch.setattr(setup, "derive_l2_api_credentials", _fail_derive)
    res = setup.build_credentials_setup(write_env_file=False)
    assert res["action"] == "skipped_derivation"
    assert res["derived"] is False
    assert res["l2"]["api_key_present"] is True


def test_write_generated_env_file_chmod_600(tmp_path):
    cfg = SimpleNamespace(
        private_key="0x" + "c" * 64,
        funder="0xfunder",
        signature_type=3,
        chain_id=137,
        clob_host="https://clob.polymarket.com",
    )
    creds = FakeApiCreds()
    target = tmp_path / ".polymarket.env.generated"
    meta = setup.write_generated_env_file(cfg, creds, str(target))
    assert meta["mode_is_600"] is True
    content = target.read_text(encoding="utf-8")
    assert "POLYMARKET_API_KEY=derived-api-key-secret-value" in content
    assert target.exists()
