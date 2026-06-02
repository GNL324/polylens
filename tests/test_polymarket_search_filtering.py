from unittest.mock import patch

from src.adapters.polymarket import PolymarketClient, filter_active_markets


class FakeMarketsResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return (
            b'{"markets":['
            b'{"id":"pm-knicks","conditionId":"cond-knicks","question":"Will the New York Knicks win the 2026 NBA Finals?","category":"Sports","active":true},'
            b'{"id":"pm-rihanna","conditionId":"cond-rihanna","question":"New Rihanna Album before GTA VI?","category":"Entertainment","active":true}'
            b']}'
        )


def test_keyword_knicks_filters_unrelated_entertainment():
    markets = [
        {"title": "Will the New York Knicks win the 2026 NBA Finals?", "category": "Sports"},
        {"title": "New Rihanna Album before GTA VI?", "category": "Entertainment"},
    ]

    filtered, diagnostics = filter_active_markets(markets, keyword="knicks")

    assert [market["title"] for market in filtered] == ["Will the New York Knicks win the 2026 NBA Finals?"]
    assert diagnostics["markets_discarded"] == 1
    assert diagnostics["discarded_markets"][0]["discard_reasons"] == ["keyword mismatch"]


def test_keyword_filtering_is_case_insensitive():
    markets = [{"title": "Will the NEW YORK KNICKS win the NBA Finals?", "category": "Sports"}]

    filtered, diagnostics = filter_active_markets(markets, keyword="KnIcKs")

    assert len(filtered) == 1
    assert diagnostics["markets_discarded"] == 0


def test_sport_filtering_rejects_non_basketball_markets():
    markets = [
        {"title": "Will the New York Knicks win the 2026 NBA Finals?", "category": "Sports"},
        {"title": "Will the New York Yankees win the World Series?", "category": "Sports"},
        {"title": "New Rihanna Album before GTA VI?", "category": "Entertainment"},
    ]

    filtered, diagnostics = filter_active_markets(markets, sport="basketball_nba")

    assert [market["title"] for market in filtered] == ["Will the New York Knicks win the 2026 NBA Finals?"]
    assert diagnostics["markets_discarded"] == 2
    assert all("sport mismatch" in item["discard_reasons"] for item in diagnostics["discarded_markets"])


def test_category_filtering_rejects_unrelated_categories():
    markets = [
        {"title": "Will the New York Knicks win the 2026 NBA Finals?", "category": "Sports"},
        {"title": "New Rihanna Album before GTA VI?", "category": "Entertainment"},
    ]

    filtered, diagnostics = filter_active_markets(markets, category="sports")

    assert [market["title"] for market in filtered] == ["Will the New York Knicks win the 2026 NBA Finals?"]
    assert diagnostics["discarded_markets"][0]["discard_reasons"] == ["category mismatch"]


def test_get_active_markets_applies_local_filtering(tmp_path):
    client = PolymarketClient(raw_dir=tmp_path)

    with patch("src.adapters.polymarket.urlopen", return_value=FakeMarketsResponse()):
        markets = client.get_active_markets(keyword="knicks", sport="basketball_nba", limit=10)

    assert [market["title"] for market in markets] == ["Will the New York Knicks win the 2026 NBA Finals?"]
    assert client.last_active_market_search_diagnostics["raw_markets_returned"] == 2
    assert client.last_active_market_search_diagnostics["filtered_markets_retained"] == 1
    assert client.last_active_market_search_diagnostics["markets_discarded"] == 1


@patch("src.cli.PolymarketClient")
def test_debug_polymarket_search_cli_smoke(mock_client, capsys):
    from src.cli import debug_polymarket_search

    mock_client.return_value.debug_active_market_search.return_value = {
        "raw_markets_returned": 2,
        "filtered_markets_retained": 1,
        "markets_discarded": 1,
        "discarded_markets": [{"title": "New Rihanna Album before GTA VI?", "discard_reasons": ["keyword mismatch"]}],
        "filtered_markets": [{"title": "Will the New York Knicks win the 2026 NBA Finals?"}],
    }

    result = debug_polymarket_search(keyword="knicks", sport_key="basketball_nba")

    assert result["filtered_markets_retained"] == 1
    assert "Raw markets returned: 2" in capsys.readouterr().out


@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_scan_live_arb_includes_polymarket_filter_counts(mock_poly, mock_kalshi, mock_fetch):
    from src.cli import scan_live_arb

    mock_poly.return_value.get_active_markets.return_value = [
        {"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?", "outcomePrices": "[0.45,0.55]"}
    ]
    mock_poly.return_value.last_active_market_search_diagnostics = {
        "raw_markets_returned": 2,
        "filtered_markets_retained": 1,
        "markets_discarded": 1,
    }
    mock_kalshi.return_value.get_markets.return_value = []
    mock_fetch.return_value = []

    result = scan_live_arb(sport_key="basketball_nba", keyword="knicks", limit=5, as_json=True)

    assert result["polymarket_raw_markets"] == 2
    assert result["polymarket_filtered_markets"] == 1
    assert result["polymarket_discarded_markets"] == 1
