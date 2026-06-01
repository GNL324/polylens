from unittest.mock import patch

from src.cli import build_wallet_report


@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_build_wallet_report_includes_kalshi_candidates(mock_poly_client, mock_kalshi_client):
    poly = mock_poly_client.return_value
    poly.get_public_profile.return_value = {"name": "tester"}
    poly.get_user_trades.return_value = [
        {"conditionId": "pm-btc", "title": "Will Bitcoin hit $100,000 in 2026?", "slug": "bitcoin-100000-2026", "side": "BUY", "outcome": "Yes", "size": 10, "price": 0.5, "timestamp": 1700000000}
    ]
    poly.get_user_activity.return_value = []
    poly.get_positions.return_value = []
    mock_kalshi_client.return_value.get_markets.return_value = [
        {"ticker": "KXBTC-100K-26", "title": "Will Bitcoin be above $100,000 in 2026?", "subtitle": "Bitcoin price", "category": "Crypto"}
    ]

    report = build_wallet_report("0x" + "1" * 40, include_kalshi=True)
    assert report.cross_platform_arbitrage_candidates
    assert report.to_dict()["cross_platform_arbitrage_candidates"][0]["kalshi_ticker"] == "KXBTC-100K-26"
