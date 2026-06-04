from unittest.mock import Mock, patch

from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthenticatedClient
from src.trading.kalshi_live_smoke import live_smoke_test_gates, run_kalshi_live_smoke_test


def _enable(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("KALSHI_LIVE_SMOKE_TEST", "true")


def test_smoke_blocked_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    result = run_kalshi_live_smoke_test(ticker="KXTEST", side="yes", price=1, count=1)
    assert result["mode"] == "blocked"
    assert "LIVE_TRADING" in result["reason"]


def test_smoke_blocked_when_live_false(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("KALSHI_LIVE_SMOKE_TEST", "true")
    accepted, reason, _ = live_smoke_test_gates(ticker="KXTEST", side="yes", price=1, count=1)
    assert accepted is False
    assert "LIVE_TRADING" in reason


def test_smoke_blocked_when_dry_run_true(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("KALSHI_LIVE_SMOKE_TEST", "true")
    accepted, reason, _ = live_smoke_test_gates(ticker="KXTEST", side="yes", price=1, count=1)
    assert accepted is False
    assert "DRY_RUN" in reason


def test_smoke_blocked_when_smoke_flag_false(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("KALSHI_LIVE_SMOKE_TEST", "false")
    accepted, reason, _ = live_smoke_test_gates(ticker="KXTEST", side="yes", price=1, count=1)
    assert accepted is False
    assert "KALSHI_LIVE_SMOKE_TEST" in reason


def test_smoke_blocked_when_notional_exceeds_max(monkeypatch):
    _enable(monkeypatch)
    accepted, reason, risk = live_smoke_test_gates(ticker="KXTEST", side="yes", price=50, count=3, max_notional=1)
    assert accepted is False
    assert reason == "notional exceeds max-notional"
    assert risk["notional"] == 1.5


def test_smoke_validates_side_price_count_ticker(monkeypatch):
    _enable(monkeypatch)
    assert live_smoke_test_gates(ticker="", side="yes", price=1, count=1)[1] == "ticker is required"
    assert live_smoke_test_gates(ticker="KX", side="maybe", price=1, count=1)[1] == "side must be yes or no"
    assert live_smoke_test_gates(ticker="KX", side="yes", price=0, count=1)[1] == "price must be positive"
    assert live_smoke_test_gates(ticker="KX", side="yes", price=1, count=0)[1] == "count must be a positive integer"


def test_smoke_builds_post_only_payload(monkeypatch):
    _enable(monkeypatch)
    client = Mock()
    client.create_order_v2_smoke_test_only.return_value = {"order": {"order_id": "ord-1", "status": "resting", "filled_count": 0}}
    client.get_order_v2_smoke_test_only.side_effect = [
        {"order": {"order_id": "ord-1", "status": "resting", "filled_count": 0}},
        {"order": {"order_id": "ord-1", "status": "canceled", "filled_count": 0}},
    ]
    client.cancel_order_v2_smoke_test_only.return_value = {"order": {"order_id": "ord-1", "status": "canceled"}}
    result = run_kalshi_live_smoke_test(ticker="KXTEST", side="yes", price=1, count=1, client=client)
    assert result["accepted"] is True
    args, kwargs = client.create_order_v2_smoke_test_only.call_args
    assert args[:4] == ("KXTEST", "yes", 0.01, 1)
    assert kwargs["client_order_id"].startswith("polylens-smoke-")
    assert result["post_only"] is True


def test_adapter_create_order_payload_is_post_only(tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    client = KalshiAuthenticatedClient(KalshiAuthConfig(api_key_id="id", private_key_path=key_path, env="production"), raw_dir=tmp_path)
    with patch.object(client, "_request_smoke_test_json", return_value={"order": {"order_id": "ord-1"}}) as request:
        client.create_order_v2_smoke_test_only("KXTEST", "no", 1, 1, client_order_id="polylens-smoke-test")
    method, endpoint = request.call_args.args[:2]
    payload = request.call_args.kwargs["json_body"]
    assert method == "POST"
    assert endpoint == "portfolio/orders"
    assert payload["post_only"] is True
    assert payload["client_order_id"] == "polylens-smoke-test"
    assert payload["no_price"] == 1
    assert payload["count"] == 1


def test_smoke_cancels_created_order(monkeypatch):
    _enable(monkeypatch)
    client = Mock()
    client.create_order_v2_smoke_test_only.return_value = {"order": {"id": "ord-2", "status": "resting"}}
    client.get_order_v2_smoke_test_only.side_effect = [{"order": {"id": "ord-2", "status": "resting"}}, {"order": {"id": "ord-2", "status": "canceled"}}]
    client.cancel_order_v2_smoke_test_only.return_value = {"order": {"id": "ord-2", "status": "canceled"}}
    result = run_kalshi_live_smoke_test(ticker="KXTEST", side="no", price=1, count=1, client=client)
    assert result["verified_canceled"] is True
    client.cancel_order_v2_smoke_test_only.assert_called_once_with("ord-2")


def test_smoke_detects_partial_fill(monkeypatch):
    _enable(monkeypatch)
    client = Mock()
    client.create_order_v2_smoke_test_only.return_value = {"order": {"order_id": "ord-3", "status": "resting", "filled_count": 0}}
    client.get_order_v2_smoke_test_only.side_effect = [{"order": {"order_id": "ord-3", "status": "partially_filled", "filled_count": 1}}, {"order": {"order_id": "ord-3", "status": "canceled", "filled_count": 1}}]
    client.cancel_order_v2_smoke_test_only.return_value = {"order": {"order_id": "ord-3", "status": "canceled"}}
    result = run_kalshi_live_smoke_test(ticker="KXTEST", side="yes", price=1, count=1, client=client)
    assert result["accepted"] is False
    assert result["mode"] == "partial_fill_detected"
    assert "review account immediately" in result["reason"]


def test_normal_order_methods_remain_write_blocked(tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    client = KalshiAuthenticatedClient(KalshiAuthConfig(api_key_id="id", private_key_path=key_path, env="production"), raw_dir=tmp_path)
    assert client.place_order()["mode"] == "write_blocked"
    assert client.cancel_order("order")["mode"] == "write_blocked"


@patch("src.cli.run_kalshi_live_smoke_test")
def test_kalshi_live_smoke_cli_smoke(mock_run, capsys):
    from src.cli import kalshi_live_smoke_test
    mock_run.return_value = {"accepted": False, "mode": "blocked", "reason": "LIVE_TRADING=true is required"}
    result = kalshi_live_smoke_test("KXTEST", "yes", 1, 1, max_notional=1, as_json=False)
    assert result["mode"] == "blocked"
    assert "LIVE_TRADING" in capsys.readouterr().out
