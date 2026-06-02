from datetime import datetime, timezone
from unittest.mock import patch

from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.opportunity_scoring import filter_scored_candidates, score_candidate, score_candidates

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)


def base_candidate(**overrides):
    candidate = {
        "estimated_edge": 0.05,
        "confidence_band": "high",
        "liquidity": 5000,
        "last_update": "2026-06-02T11:30:00Z",
        "close_time": "2026-06-04T12:00:00Z",
        "pricing_status": "priced",
        "venue_pair": "polymarket_kalshi",
    }
    candidate.update(overrides)
    return candidate


def test_high_edge_high_confidence_scores_above_low_confidence():
    high = score_candidate(base_candidate(estimated_edge=0.08, confidence_band="high"), now=NOW)
    low = score_candidate(base_candidate(estimated_edge=0.08, confidence_band="low"), now=NOW)
    assert high["execution_score"] > low["execution_score"]
    assert high["raw_edge"] == 0.08


def test_stale_price_penalty():
    fresh = score_candidate(base_candidate(last_update="2026-06-02T11:45:00Z"), now=NOW)
    stale = score_candidate(base_candidate(last_update="2026-05-28T11:45:00Z"), now=NOW)
    assert stale["freshness_score"] < fresh["freshness_score"]
    assert stale["execution_score"] < fresh["execution_score"]
    assert "stale price" in stale["score_reason"]


def test_low_liquidity_penalty():
    deep = score_candidate(base_candidate(liquidity=10000), now=NOW)
    thin = score_candidate(base_candidate(liquidity=0), now=NOW)
    assert thin["liquidity_score"] < deep["liquidity_score"]
    assert thin["execution_score"] < deep["execution_score"]
    assert "low liquidity" in thin["score_reason"]


def test_soon_to_close_penalty():
    later = score_candidate(base_candidate(close_time="2026-06-04T12:00:00Z"), now=NOW)
    soon = score_candidate(base_candidate(close_time="2026-06-02T12:30:00Z"), now=NOW)
    assert soon["time_score"] < later["time_score"]
    assert soon["execution_score"] < later["execution_score"]
    assert "soon to close" in soon["score_reason"]


def test_filter_by_min_edge():
    scored = score_candidates([base_candidate(estimated_edge=0.01), base_candidate(estimated_edge=0.05)], now=NOW)
    filtered, reasons = filter_scored_candidates(scored, min_edge=0.03, include_low_confidence=True, now=NOW)
    assert len(filtered) == 1
    assert filtered[0]["estimated_edge"] == 0.05
    assert reasons["below min edge"] == 1


def test_filter_by_min_score():
    scored = score_candidates([base_candidate(estimated_edge=0.01, liquidity=0), base_candidate(estimated_edge=0.09, liquidity=10000)], now=NOW)
    filtered, reasons = filter_scored_candidates(scored, min_score=0.6, include_low_confidence=True, now=NOW)
    assert len(filtered) == 1
    assert filtered[0]["estimated_edge"] == 0.09
    assert reasons["below min execution score"] == 1


def test_candidate_sorting_by_execution_score():
    scored = score_candidates([base_candidate(estimated_edge=0.02), base_candidate(estimated_edge=0.1)], now=NOW)
    assert scored[0]["execution_score"] >= scored[1]["execution_score"]
    assert scored[0]["estimated_edge"] == 0.1


def test_live_scanner_filters_and_sorts_by_execution_score():
    result = scan_live_arbitrage(
        [
            {"conditionId": "pm-lakers", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "outcomePrices": "[0.5,0.5]", "updatedAt": "2026-06-02T11:30:00Z"},
            {"conditionId": "pm-knicks", "title": "Will the New York Knicks beat the Boston Celtics?", "outcomePrices": "[0.3,0.7]", "updatedAt": "2026-06-02T11:30:00Z"},
        ],
        [],
        sportsbook_lines=[
            {"event_id": "evt1", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.55, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z", "last_update": "2026-06-02T11:30:00Z"},
            {"event_id": "evt2", "league": "NBA", "team": "New York Knicks", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.7, "bookmaker_name": "Book", "commence_time": "2026-06-11T00:00:00Z", "last_update": "2026-06-02T11:30:00Z"},
        ],
        min_edge=0.1,
        include_low_confidence=True,
    )
    assert result["candidates_before_filtering"] == 2
    assert result["candidates_after_filtering"] == 1
    assert result["filter_reasons"]["below min edge"] == 1
    assert result["top_candidates"][0]["estimated_edge"] == 0.4
    assert "execution_score" in result["top_candidates"][0]


@patch("src.cli.fetch_odds")
@patch("src.cli.KalshiClient")
@patch("src.cli.PolymarketClient")
def test_scan_live_arb_scoring_flags_cli_smoke(mock_poly, mock_kalshi, mock_fetch, capsys):
    from src.cli import scan_live_arb

    mock_poly.return_value.get_active_markets.return_value = [{"conditionId": "pm-lakers", "title": "Will the Los Angeles Lakers beat the Boston Celtics?", "outcomePrices": "[0.45,0.55]", "updatedAt": "2026-06-02T11:30:00Z"}]
    mock_kalshi.return_value.get_markets.return_value = []
    mock_fetch.return_value = [{"event_id": "evt", "league": "NBA", "team": "Los Angeles Lakers", "opponent": "Boston Celtics", "market_type": "game_winner", "implied_probability": 0.6, "bookmaker_name": "Book", "commence_time": "2026-06-10T00:00:00Z", "last_update": "2026-06-02T11:30:00Z"}]
    result = scan_live_arb(sport_key="basketball_nba", limit=5, min_edge=0.01, min_score=0.1, max_close_hours=500, include_low_confidence=True)
    out = capsys.readouterr().out
    assert "Candidates before/after filtering" in out
    assert result["candidates_after_filtering"] == 1
    assert result["top_candidates"][0]["execution_score"] is not None
