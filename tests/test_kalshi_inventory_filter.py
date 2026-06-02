from unittest.mock import patch

from src.analysis.kalshi_inventory_filter import classify_kalshi_inventory_product, filter_kalshi_inventory


def market(**overrides):
    row = {"ticker": "KXNBAFINAL-KNICKS", "event_ticker": "KXNBAFINAL", "title": "Will the New York Knicks win the NBA Finals?"}
    row.update(overrides)
    return row


def test_kalshi_inventory_filter_rejects_multigame_extended_prefix():
    row = market(ticker="KXMVESPORTSMULTIGAMEEXTENDED-S2026ABC", title="Sports multigame extended bundle")

    retained, diagnostics = filter_kalshi_inventory([row])

    assert retained == []
    assert diagnostics["kalshi_markets_discarded"] == 1
    assert diagnostics["discarded_sample"][0]["product_type"] == "sportsbook_style_same_game_parlay"
    assert diagnostics["discarded_sample"][0]["discard_reason"] == "rejected sportsbook_style_same_game_parlay"


def test_kalshi_inventory_filter_rejects_cross_category_prefix():
    row = market(ticker="KXMVECROSSCATEGORY-123", title="Cross category bundle")

    retained, diagnostics = filter_kalshi_inventory([row])

    assert retained == []
    assert diagnostics["discarded_sample"][0]["product_type"] == "cross_category"
    assert diagnostics["discarded_reason_counts"] == {"rejected cross_category": 1}


def test_kalshi_inventory_filter_rejects_player_prop_bundle():
    row = market(ticker="KXPLAYERBUNDLE", title="NBA player points parlay bundle")

    assert classify_kalshi_inventory_product(row) == "player_prop_bundle"
    retained, diagnostics = filter_kalshi_inventory([row])
    assert retained == []
    assert diagnostics["discarded_reason_counts"] == {"rejected player_prop_bundle": 1}


def test_kalshi_inventory_filter_retains_outright_and_game_markets():
    rows = [
        market(ticker="KXNBAFINAL-KNICKS", title="Will the New York Knicks win the NBA Finals?"),
        market(ticker="KXLALBOS", title="Will the Los Angeles Lakers beat the Boston Celtics?"),
        market(ticker="KXMVECROSSCATEGORY-123", title="Cross category bundle"),
    ]

    retained, diagnostics = filter_kalshi_inventory(rows)

    assert [row["ticker"] for row in retained] == ["KXNBAFINAL-KNICKS", "KXLALBOS"]
    assert diagnostics["kalshi_markets_fetched"] == 3
    assert diagnostics["kalshi_markets_retained"] == 2
    assert diagnostics["kalshi_markets_discarded"] == 1
    assert diagnostics["retained_sample"][0]["product_type"] == "championship_winner"


def test_kalshi_inventory_filter_rejects_unknown_but_keeps_crypto_single_leg():
    unknown = market(ticker="KXRANDOM", title="Will something vague happen?")
    crypto = market(ticker="KXBTC-100K", title="Will BTC be above $100,000 on June 30?")

    retained, diagnostics = filter_kalshi_inventory([unknown, crypto])

    assert [row["ticker"] for row in retained] == ["KXBTC-100K"]
    assert diagnostics["discarded_reason_counts"] == {"rejected unknown": 1}


@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_scan_live_arb_filters_kalshi_inventory_before_matching(mock_poly, mock_kalshi, mock_fetch):
    from src.cli import scan_live_arb

    mock_poly.return_value.get_active_markets.return_value = [
        {"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?", "outcomePrices": "[0.45,0.55]"}
    ]
    mock_poly.return_value.last_active_market_search_diagnostics = {}
    mock_kalshi.return_value.get_markets.return_value = [
        market(ticker="KXNBAFINAL-KNICKS", title="Will the New York Knicks win the NBA Finals?", yes_ask_dollars="0.5"),
        market(ticker="KXMVESPORTSMULTIGAMEEXTENDED-S2026ABC", title="Sports multigame extended bundle"),
        market(ticker="KXMVECROSSCATEGORY-123", title="Cross category bundle"),
    ]
    mock_fetch.return_value = []

    result = scan_live_arb(sport_key="basketball_nba", keyword="knicks", limit=5, as_json=True)

    assert result["kalshi_markets_fetched"] == 3
    assert result["kalshi_markets_retained"] == 1
    assert result["kalshi_markets_discarded"] == 2
    assert result["kalshi_inventory_discarded_reason_counts"] == {
        "rejected sportsbook_style_same_game_parlay": 1,
        "rejected cross_category": 1,
    }


@patch("src.cli.KalshiClient")
def test_debug_kalshi_inventory_cli_smoke(mock_kalshi, capsys):
    from src.cli import debug_kalshi_inventory

    mock_kalshi.return_value.get_markets.return_value = [
        market(ticker="KXNBAFINAL-KNICKS", title="Will the New York Knicks win the NBA Finals?"),
        market(ticker="KXMVECROSSCATEGORY-123", title="Cross category bundle"),
    ]

    result = debug_kalshi_inventory(limit=5)

    assert result["kalshi_markets_retained"] == 1
    assert result["kalshi_markets_discarded"] == 1
    assert "Kalshi markets fetched: 2" in capsys.readouterr().out


@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_explain_live_matches_reports_kalshi_inventory_filter_separately(mock_poly, mock_kalshi, mock_fetch):
    from src.cli import explain_live_matches_cli

    mock_poly.return_value.get_active_markets.return_value = []
    mock_kalshi.return_value.get_markets.return_value = [
        market(ticker="KXMVECROSSCATEGORY-123", title="Cross category bundle"),
    ]
    mock_fetch.return_value = []

    result = explain_live_matches_cli(sport_key="basketball_nba", as_json=True)

    assert result["matches_attempted"] == 0
    assert result["kalshi_inventory_filter"]["kalshi_markets_discarded"] == 1
    assert result["kalshi_inventory_filter"]["discarded_sample"][0]["discard_reason"] == "rejected cross_category"
