import json
from unittest.mock import patch

from src.analysis.market_inventory import summarize_market_inventory


def test_inventory_counts_market_categories():
    pm = [
        {"title": "Bitcoin up on June 30", "status": "active"},
        {"title": "Will the Lakers win the 2026 NBA Finals?", "status": "closed", "resolved": True},
        {"title": "Will it rain tomorrow?", "status": None},
    ]
    kalshi = [{"ticker": "KXBTC", "title": "Will BTC be higher by close on June 30?", "status": "active", "close_time": "2026-06-30T21:00:00Z"}]
    inventory = summarize_market_inventory(pm, kalshi)
    assert inventory["polymarket"]["category_counts"]["Crypto"] == 1
    assert inventory["polymarket"]["category_counts"]["Sports"] == 1
    assert inventory["polymarket_open_count"] == 1
    assert inventory["polymarket_closed_count"] == 1


def test_inventory_detects_crypto_markets():
    inventory = summarize_market_inventory([{"title": "Ethereum below $4,000 on July 31?"}], [])
    assert inventory["polymarket"]["asset_counts"] == {"ETH": 1}
    assert inventory["polymarket"]["crypto_market_types_found"]["price target"] == 1


def test_inventory_detects_sports_markets():
    inventory = summarize_market_inventory([{"title": "Will the Yankees beat the Dodgers tonight?"}], [])
    assert inventory["polymarket"]["league_counts"] == {"MLB": 1}
    assert inventory["polymarket"]["sports_market_types_found"]["game_winner"] == 1


def test_inventory_reports_unparsed_examples():
    inventory = summarize_market_inventory([{"title": "Will it rain tomorrow?", "condition_id": "pm-weather"}], [])
    assert inventory["unparsed_polymarket_count"] == 1
    assert inventory["polymarket"]["unparsed_examples"][0]["id"] == "pm-weather"


def test_explain_matches_includes_inventory_summary():
    from src.analysis.match_diagnostics import explain_market_matches

    trades = [{"conditionId": "pm-btc", "title": "Bitcoin up on June 30", "outcome": "Up", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXETH", "title": "Will ETH be higher by close on June 30?", "close_time": "2026-06-30T21:00:00Z"}]
    result = explain_market_matches(trades, kalshi)
    assert "inventory_summary" in result
    assert "likely_zero_candidate_reason" in result
    assert result["unparsed_polymarket_count"] == 0


@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_market_inventory_json_output_is_valid(mock_poly_client, mock_kalshi_client, capsys):
    from src.cli import market_inventory

    mock_poly_client.return_value.get_wallet_markets.return_value = [{"title": "Bitcoin up on June 30", "status": "active"}]
    mock_kalshi_client.return_value.get_markets.return_value = [{"ticker": "KXBTC", "title": "Will BTC be higher by close on June 30?", "status": "active", "close_time": "2026-06-30T21:00:00Z"}]
    result = market_inventory("0x" + "5" * 40, as_json=True)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["polymarket"]["total_count"] == 1
    assert result["kalshi"]["total_count"] == 1
