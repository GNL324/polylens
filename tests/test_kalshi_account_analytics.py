from unittest.mock import patch

from src.analysis.kalshi_account_analytics import build_kalshi_account_report, detect_kalshi_patterns, export_kalshi_report
from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthenticatedClient


def _payloads():
    balance = {"balance": 12345}
    positions = {
        "positions": [
            {"ticker": "KXBTCUP", "position": 2, "realized_pnl": 350, "unrealized_pnl": 25, "title": "Bitcoin up today"},
            {"ticker": "KXETHDOWN", "position": 0, "realized_pnl": -50, "title": "Ethereum down today"},
        ]
    }
    orders = {"orders": [{"ticker": "KXBTCUP", "status": "filled", "side": "yes", "price": 45, "count": 2, "fee": 5, "realized_pnl": 350, "title": "Bitcoin up today"}]}
    fills = {
        "fills": [
            {"ticker": "KXBTCUP", "side": "yes", "price": 45, "count": 2, "fee": 5, "realized_pnl": 350, "title": "Bitcoin up today"},
            {"ticker": "KXBTCUP", "side": "no", "price": 48, "count": 1, "fee": 2, "realized_pnl": -100, "title": "Bitcoin up today"},
            {"ticker": "KXETHDOWN", "side": "yes", "price": 55, "count": 4, "fee": 4, "realized_pnl": 200, "title": "Ethereum down today"},
        ]
    }
    return balance, positions, orders, fills


def test_build_kalshi_account_report_summarizes_pnl_fees_and_trades():
    report = build_kalshi_account_report(*_payloads())
    assert report["summary"]["account_balance"] == 123.45
    assert report["summary"]["open_position_count"] == 1
    assert report["summary"]["realized_pnl"] == 3.0
    assert report["summary"]["fees_paid"] == 0.11
    assert report["summary"]["trade_count"] == 3
    assert report["summary"]["win_rate"] == 0.6667
    assert report["summary"]["average_entry_price"] == 0.4933
    assert report["summary"]["average_contract_size"] == 2.3333
    assert report["trades_by_asset"]["BTC"] == 2
    assert report["trades_by_asset"]["ETH"] == 1
    assert report["trades_by_market_type"]["crypto_price_direction"] == 3


def test_detect_kalshi_patterns_flags_repeated_and_possible_arb():
    report = build_kalshi_account_report(*_payloads())
    patterns = detect_kalshi_patterns(report)
    assert patterns["repeated_trading_behavior"]
    assert patterns["possible_arbitrage_behavior"]
    assert patterns["behavior_classification"] == "possible_arbitrage"
    assert patterns["high_roi_trades"]
    assert patterns["worst_trades"][0]["ticker"] == "KXBTCUP"


def test_export_kalshi_report_writes_json_and_csv(tmp_path):
    report = build_kalshi_account_report(*_payloads())
    files = export_kalshi_report(report, tmp_path)
    assert files["json"].endswith("kalshi_report.json")
    assert files["csv"].endswith("kalshi_report.csv")
    assert "realized_pnl" in (tmp_path / "kalshi_report.csv").read_text(encoding="utf-8")


@patch("src.cli.KalshiAuthenticatedClient")
def test_kalshi_report_cli_smoke(mock_client, capsys):
    from src.cli import kalshi_report

    balance, positions, orders, fills = _payloads()
    client = mock_client.return_value
    client.get_balance.return_value = balance
    client.get_positions.return_value = positions
    client.get_orders.return_value = orders
    client.get_fills.return_value = fills
    report = kalshi_report(as_json=False)
    assert report["summary"]["trade_count"] == 3
    assert "Kalshi Account Report" in capsys.readouterr().out


@patch("src.cli.KalshiAuthenticatedClient")
def test_kalshi_patterns_cli_smoke(mock_client):
    from src.cli import kalshi_patterns

    balance, positions, orders, fills = _payloads()
    client = mock_client.return_value
    client.get_balance.return_value = balance
    client.get_positions.return_value = positions
    client.get_orders.return_value = orders
    client.get_fills.return_value = fills
    patterns = kalshi_patterns(as_json=True)
    assert patterns["behavior_classification"] == "possible_arbitrage"


@patch("src.cli.export_kalshi_report")
@patch("src.cli.KalshiAuthenticatedClient")
def test_kalshi_export_cli_smoke(mock_client, mock_export):
    from src.cli import kalshi_export

    balance, positions, orders, fills = _payloads()
    client = mock_client.return_value
    client.get_balance.return_value = balance
    client.get_positions.return_value = positions
    client.get_orders.return_value = orders
    client.get_fills.return_value = fills
    mock_export.return_value = {"json": "data/reports/kalshi_report.json", "csv": "data/reports/kalshi_report.csv"}
    result = kalshi_export(as_json=True)
    assert result["accepted"] is True
    assert result["files"]["json"].endswith("kalshi_report.json")


def test_write_endpoints_remain_blocked(tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    client = KalshiAuthenticatedClient(KalshiAuthConfig(api_key_id="id", private_key_path=key_path, env="production"), raw_dir=tmp_path)
    assert client.place_order()["mode"] == "write_blocked"
    assert client.cancel_order("order")["mode"] == "write_blocked"
