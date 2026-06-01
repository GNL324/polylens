from src.analysis.cross_market import compare_wallet_markets_to_kalshi, score_market_pair
from src.analysis.market_normalization import normalize_kalshi_market, normalize_polymarket_market
from src.analysis.sports_parser import parse_market_record, parse_sports_market_text
from src.analysis.structured_matching import structured_sports_candidates


def test_sports_parser_extracts_core_fields():
    parsed = parse_sports_market_text("Will the Lakers win the 2026 NBA Finals?")
    assert parsed.league == "NBA"
    assert parsed.team == "los angeles lakers"
    assert parsed.season_year == 2026
    assert parsed.event_type == "finals"
    assert parsed.market_type == "championship_winner"


def test_structured_matching_adds_nba_finals_candidate_beyond_text_similarity():
    pm = [{"conditionId": "pm-nba", "title": "Will the Lakers win the 2026 NBA Finals?", "size": 10, "price": 0.4}]
    kalshi = [{"ticker": "KXNBA-LA-2026", "title": "Los Angeles basketball title in 2026", "subtitle": "NBA champion"}]
    text_only = score_market_pair(normalize_polymarket_market(pm[0]), normalize_kalshi_market(kalshi[0]))
    structured = compare_wallet_markets_to_kalshi(pm, kalshi)
    assert text_only is None
    assert len(structured) == 1
    assert structured[0]["sport_league_category_guess"]["league"] == "NBA"
    assert structured[0]["confidence_band"] in {"medium", "high"}


def test_structured_matching_mlb_game_market():
    pm = [{"conditionId": "pm-mlb", "title": "Will the Yankees beat the Dodgers tonight?", "slug": "mlb-yankees-dodgers", "size": 5, "price": 0.55}]
    kalshi = [{"ticker": "KXMLB-NYY-LAD", "title": "New York vs Los Angeles baseball game winner", "subtitle": "MLB"}]
    candidates = structured_sports_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["sport_league_category_guess"]["league"] == "MLB"
    assert "new" in candidates[0]["shared_keywords_entities"] or "york" in candidates[0]["shared_keywords_entities"]


def test_structured_matching_nhl_futures_market():
    pm = [{"conditionId": "pm-nhl", "title": "Will the Oilers win the 2026 Stanley Cup?", "size": 8, "price": 0.2}]
    kalshi = [{"ticker": "KXNHL-EDM-2026", "title": "Edmonton hockey championship in 2026", "subtitle": "NHL Stanley Cup winner"}]
    candidates = structured_sports_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["sport_league_category_guess"]["league"] == "NHL"
    assert candidates[0]["structured_match"]["polymarket"]["market_type"] == "championship_winner"


def test_structured_matching_rejects_mismatched_team():
    pm = [{"conditionId": "pm-lakers", "title": "Will the Lakers win the 2026 NBA Finals?", "size": 1, "price": 0.4}]
    kalshi = [{"ticker": "KXNBA-BOS-2026", "title": "Boston basketball title in 2026", "subtitle": "NBA champion"}]
    assert structured_sports_candidates(pm, kalshi) == []


def test_structured_matching_rejects_mismatched_league():
    pm = [{"conditionId": "pm-nba", "title": "Will the Lakers win the 2026 NBA Finals?", "size": 1, "price": 0.4}]
    kalshi = [{"ticker": "KXMLB-LA-2026", "title": "Los Angeles baseball title in 2026", "subtitle": "MLB champion"}]
    assert structured_sports_candidates(pm, kalshi) == []
