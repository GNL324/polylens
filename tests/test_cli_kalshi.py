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


@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_price_aware_report_uses_structured_sports_matches(mock_poly_client, mock_kalshi_client):
    poly = mock_poly_client.return_value
    poly.get_public_profile.return_value = {"name": "sports"}
    poly.get_user_trades.return_value = [
        {"conditionId": "pm-lakers", "title": "Will the Lakers win the 2026 NBA Finals?", "side": "BUY", "outcome": "Yes", "outcomeIndex": 0, "size": 10, "price": 0.4, "timestamp": 1700000000}
    ]
    poly.get_user_activity.return_value = []
    poly.get_positions.return_value = []
    mock_kalshi_client.return_value.get_markets.return_value = [
        {"ticker": "KXNBA-LA-2026", "title": "Los Angeles basketball title in 2026", "subtitle": "NBA champion", "yes_ask_dollars": "0.7000", "no_ask_dollars": "0.5500", "yes_bid_dollars": "0.6900", "last_price_dollars": "0.6900", "liquidity_dollars": "10.0000"}
    ]

    report = build_wallet_report("0x" + "2" * 40, include_kalshi=True, include_pricing=True)
    assert report.cross_platform_arbitrage_candidates
    assert report.cross_platform_arbitrage_candidates[0]["structured_match"]
    assert report.price_aware_arbitrage_candidates
    assert report.price_aware_arbitrage_candidates[0]["kalshi_ticker"] == "KXNBA-LA-2026"


@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_price_aware_report_uses_structured_crypto_matches(mock_poly_client, mock_kalshi_client):
    poly = mock_poly_client.return_value
    poly.get_public_profile.return_value = {"name": "crypto"}
    poly.get_user_trades.return_value = [
        {"conditionId": "pm-btc-100k", "title": "Will Bitcoin hit $100k by June 30?", "side": "BUY", "outcome": "Yes", "outcomeIndex": 0, "size": 10, "price": 0.4, "timestamp": 1700000000}
    ]
    poly.get_user_activity.return_value = []
    poly.get_positions.return_value = []
    mock_kalshi_client.return_value.get_markets.return_value = [
        {"ticker": "KXBTC-100K-JUN30", "title": "Will BTC be above $100,000 on June 30?", "close_time": "2026-06-30T23:59:00Z", "yes_ask_dollars": "0.7000", "no_ask_dollars": "0.5500", "yes_bid_dollars": "0.6900", "last_price_dollars": "0.6900", "liquidity_dollars": "10.0000"}
    ]

    report = build_wallet_report("0x" + "3" * 40, include_kalshi=True, include_pricing=True)
    assert report.cross_platform_arbitrage_candidates
    assert report.cross_platform_arbitrage_candidates[0]["structured_match"]["polymarket"]["asset_symbol"] == "BTC"
    assert report.price_aware_arbitrage_candidates
    assert report.price_aware_arbitrage_candidates[0]["kalshi_ticker"] == "KXBTC-100K-JUN30"
