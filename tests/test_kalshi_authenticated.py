from unittest.mock import patch

import pytest

from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthConfigError, KalshiAuthenticatedClient


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def _config(tmp_path, env="demo"):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n", encoding="utf-8")
    return KalshiAuthConfig(api_key_id="test-key-id", private_key_path=key_path, env=env)


def test_authenticated_balance_uses_signed_headers(tmp_path):
    config = _config(tmp_path)
    client = KalshiAuthenticatedClient(config=config, raw_dir=tmp_path)
    with patch.object(KalshiAuthenticatedClient, "_sign_request", return_value="signed"), patch("src.adapters.kalshi.urlopen", return_value=FakeResponse(b'{"balance":12345}')) as mock_urlopen:
        result = client.get_balance()
    request = mock_urlopen.call_args.args[0]
    assert result["balance"] == 12345
    assert request.headers["Kalshi-access-key"] == "test-key-id"
    assert request.headers["Kalshi-access-signature"] == "signed"
    assert request.full_url == "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance"


def test_authenticated_positions_are_paginated(tmp_path):
    config = _config(tmp_path)
    client = KalshiAuthenticatedClient(config=config, raw_dir=tmp_path)
    responses = [
        FakeResponse(b'{"positions":[{"ticker":"KX1"}],"cursor":"next"}'),
        FakeResponse(b'{"positions":[{"ticker":"KX2"}],"cursor":""}'),
    ]
    with patch.object(KalshiAuthenticatedClient, "_sign_request", return_value="signed"), patch("src.adapters.kalshi.urlopen", side_effect=responses):
        result = client.get_positions(limit=1, max_pages=2)
    assert [row["ticker"] for row in result["positions"]] == ["KX1", "KX2"]
    assert result["pages_fetched"] == 2


def test_authenticated_orders_and_fills_use_read_only_get(tmp_path):
    config = _config(tmp_path)
    client = KalshiAuthenticatedClient(config=config, raw_dir=tmp_path)
    with patch.object(KalshiAuthenticatedClient, "_sign_request", return_value="signed"), patch("src.adapters.kalshi.urlopen", return_value=FakeResponse(b'{"orders":[],"cursor":""}')):
        assert client.get_orders()["orders"] == []
    with patch.object(KalshiAuthenticatedClient, "_sign_request", return_value="signed"), patch("src.adapters.kalshi.urlopen", return_value=FakeResponse(b'{"fills":[],"cursor":""}')):
        assert client.get_fills()["fills"] == []


def test_missing_key_path(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(KalshiAuthConfigError, match="KALSHI_PRIVATE_KEY_PATH"):
        KalshiAuthConfig.from_env()


def test_bad_key_path(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "missing.key"))
    with pytest.raises(KalshiAuthConfigError, match="not found"):
        KalshiAuthConfig.from_env()


def test_bad_api_key_id(monkeypatch, tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key_path))
    with pytest.raises(KalshiAuthConfigError, match="KALSHI_API_KEY_ID"):
        KalshiAuthConfig.from_env()


def test_write_endpoint_blocked_without_network(tmp_path):
    config = _config(tmp_path)
    client = KalshiAuthenticatedClient(config=config, raw_dir=tmp_path)
    with patch("src.adapters.kalshi.urlopen") as mock_urlopen:
        result = client.place_order(ticker="KX", side="yes", price=0.5, count=1)
    assert result["mode"] == "write_blocked"
    mock_urlopen.assert_not_called()


def test_production_env_is_still_read_only(tmp_path):
    config = _config(tmp_path, env="production")
    client = KalshiAuthenticatedClient(config=config, raw_dir=tmp_path)
    assert client.base_url == "https://external-api.kalshi.com/trade-api/v2"
    assert client.cancel_order("order-id")["mode"] == "write_blocked"

@patch("src.cli.KalshiAuthenticatedClient")
def test_authenticated_cli_commands_smoke(mock_client, capsys):
    from src.cli import kalshi_account, kalshi_balance, kalshi_orders, kalshi_positions

    client = mock_client.return_value
    client.get_account.return_value = {"usage_tier": "basic"}
    client.get_balance.return_value = {"balance": 123}
    client.get_positions.return_value = {"positions": []}
    client.get_orders.return_value = {"orders": []}

    assert kalshi_account(as_json=True)["usage_tier"] == "basic"
    assert kalshi_balance(as_json=True)["balance"] == 123
    assert kalshi_positions(limit=2, as_json=True)["positions"] == []
    assert kalshi_orders(limit=2, as_json=True)["orders"] == []
    output = capsys.readouterr().out
    assert "usage_tier" in output
    assert "balance" in output
    client.get_positions.assert_called_with(limit=2)
    client.get_orders.assert_called_with(limit=2)

