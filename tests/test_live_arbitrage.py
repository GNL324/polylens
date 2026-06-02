from unittest.mock import patch

from src.adapters.odds_api import MissingOddsAPIKey
from src.adapters.polymarket import PolymarketClient
from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.live_market_discovery import normalize_kalshi_live_markets, normalize_polymarket_live_markets, normalize_sportsbook_live_lines


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'{"markets":[{"id":"pm1","conditionId":"cond1","question":"Will Bitcoin hit $100k by June 30?","outcomePrices":"[0.42,0.58]","active":true}]}'


def test_live_polymarket_discovery_parsing(tmp_path):
    client = PolymarketClient(raw_dir=tmp_path)
    with patch("src.adapters.polymarket.urlopen", return_value=FakeResponse()):
        markets = client.get_active_markets(keyword="bitcoin", limit=1)
    assert markets[0]["condition_id"] == "cond1"
    assert markets[0]["title"] == "Will Bitcoin hit $100k by June 30?"
    assert markets[0]["outcome_prices"] == "[0.42,0.58]"


def test_live_market_normalization():
    pm = normalize_polymarket_live_markets([{"conditionId": "pm-btc", "title": "Will Bitcoin hit $100k by June 30?", "outcomePrices": "[0.4,0.6]"}])
    kalshi = normalize_kalshi_live_markets([{"ticker": "KXBTC-100K", "title": "Will BTC be above $100,000 on June 30?", "yes_ask_dollars": "0.45", "no_ask_dollars": "0.55"}])
    book = normalize_sportsbook_live_lines([{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.6}])
    assert pm[0]["venue"] == "polymarket"
    assert pm[0]["asset_or_team"] == "BTC"
    assert kalshi[0]["price_data"]["yes_ask"] == 0.45
    assert book[0]["venue"] == "sportsbook"


def test_polymarket_kalshi_live_matching():
    result = scan_live_arbitrage(
        [{"conditionId": "pm-btc", "title": "Will Bitcoin hit $100k by June 30?", "outcomePrices": "[0.4,0.6]"}],
        [{"ticker": "KXBTC-100K", "title": "Will BTC be above $100,000 on June 30?", "close_time": "2026-06-30T23:59:00Z", "yes_ask_dollars": "0.45", "no_ask_dollars": "0.55"}],
    )
    assert result["matches_found_by_venue_pair"]["polymarket_kalshi"] == 1
    assert result["top_candidates"][0]["venue_pair"] == "polymarket_kalshi"


def test_polymarket_sportsbook_live_matching():
    result = scan_live_arbitrage(
        [{"conditionId": "pm-lakers", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "outcomePrices": "[0.45,0.55]"}],
        [],
        sportsbook_lines=[{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.6, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z"}],
    )
    assert result["matches_found_by_venue_pair"]["polymarket_sportsbook"] == 1
    assert result["top_candidates"][0]["estimated_edge"] == 0.15


def test_kalshi_sportsbook_live_matching():
    result = scan_live_arbitrage(
        [],
        [{"ticker": "KXLALBOS", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "yes_ask_dollars": "0.48"}],
        sportsbook_lines=[{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.58, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z"}],
    )
    assert result["matches_found_by_venue_pair"]["kalshi_sportsbook"] == 1
    assert result["top_candidates"][0]["venue_pair"] == "kalshi_sportsbook"


def test_live_scanner_candidate_sorting():
    result = scan_live_arbitrage(
        [
            {"conditionId": "pm-lakers", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "outcomePrices": "[0.5,0.5]"},
            {"conditionId": "pm-knicks", "title": "Will the New York Knicks beat the Boston Celtics?", "outcomePrices": "[0.3,0.7]"},
        ],
        [],
        sportsbook_lines=[
            {"event_id": "evt1", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.55, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z"},
            {"event_id": "evt2", "league": "NBA", "team": "New York Knicks", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.7, "bookmaker_name": "Book", "commence_time": "2026-06-11T00:00:00Z"},
        ],
    )
    assert result["top_candidates"][0]["estimated_edge"] == 0.4


@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_scan_live_arb_cli_smoke(mock_poly, mock_kalshi, mock_fetch, capsys):
    from src.cli import scan_live_arb

    mock_poly.return_value.get_active_markets.return_value = [{"conditionId": "pm-lakers", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "outcomePrices": "[0.45,0.55]"}]
    mock_kalshi.return_value.get_markets.return_value = []
    mock_fetch.return_value = [{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.6, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z"}]
    result = scan_live_arb(sport_key="basketball_nba", limit=5)
    out = capsys.readouterr().out
    assert "Polylens Live Arbitrage Scan" in out
    assert result["matches_found_by_venue_pair"]["polymarket_sportsbook"] == 1


@patch("src.cli.fetch_odds", side_effect=MissingOddsAPIKey("ODDS_API_KEY is required to call The Odds API"))
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_missing_odds_key_only_blocks_sportsbook_side(mock_poly, mock_kalshi, _mock_fetch):
    from src.cli import scan_live_arb

    mock_poly.return_value.get_active_markets.return_value = [{"conditionId": "pm-btc", "title": "Will Bitcoin hit $100k by June 30?", "outcomePrices": "[0.4,0.6]"}]
    mock_kalshi.return_value.get_markets.return_value = [{"ticker": "KXBTC-100K", "title": "Will BTC be above $100,000 on June 30?", "close_time": "2026-06-30T23:59:00Z", "yes_ask_dollars": "0.45", "no_ask_dollars": "0.55"}]
    result = scan_live_arb(sport_key="basketball_nba", limit=5, as_json=True)
    assert result["markets_scanned_by_venue"]["polymarket"] == 1
    assert result["markets_scanned_by_venue"]["kalshi"] == 1
    assert "ODDS_API_KEY missing; sportsbook side skipped" in result["skipped_rejected_reason_counts"]
    assert result["matches_found_by_venue_pair"]["polymarket_kalshi"] == 1
