from unittest.mock import patch

from src.analysis.prop_arbitrage import scan_prop_arbitrage
from src.notifications.telegram import should_alert
from src.storage.opportunities import load_recent_opportunities, opportunity_stats, save_opportunity


def opp(over=0.45, under=0.50):
    return [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "odds": 120, "implied_probability": over},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "line": 28.5, "side": "under", "bookmaker": "BookB", "odds": 120, "implied_probability": under},
    ]


def test_roi_and_profit_filtering():
    result = scan_prop_arbitrage(opp(), bankroll=1000, min_guaranteed_roi=0.10, min_profit=10)
    assert result["candidates_before_filter"] == 1
    assert result["candidates_after_filter"] == 0


def test_opportunity_ranking_score_and_sorting():
    props = opp(0.45, 0.50) + [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market_type": "player_rebounds", "line": 9.5, "side": "over", "bookmaker": "BookA", "odds": 150, "implied_probability": 0.40},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market_type": "player_rebounds", "line": 9.5, "side": "under", "bookmaker": "BookB", "odds": 150, "implied_probability": 0.40},
    ]
    result = scan_prop_arbitrage(props, bankroll=1000)
    assert result["prop_arbitrage_candidates"][0]["player"] == "Josh Hart"
    assert result["prop_arbitrage_candidates"][0]["ranking_score"] > result["prop_arbitrage_candidates"][1]["ranking_score"]


def test_sqlite_persistence(tmp_path):
    db = tmp_path / "opportunities.db"
    opportunity = scan_prop_arbitrage(opp(), bankroll=1000)["prop_arbitrage_candidates"][0]
    assert save_opportunity(opportunity, db_path=str(db), sport="basketball_nba") is True
    assert save_opportunity(opportunity, db_path=str(db), sport="basketball_nba") is False
    rows = load_recent_opportunities(db_path=str(db))
    stats = opportunity_stats(db_path=str(db))
    assert len(rows) == 1
    assert stats["total_opportunities"] == 1
    assert stats["best_roi"] > 0


def test_duplicate_alert_suppression():
    assert should_alert("abc", now=1000) is True
    assert should_alert("abc", now=1001) is False
    assert should_alert("abc", now=2000) is True


@patch("src.cli.send_telegram_alert", return_value={"sent": True, "status": "sent"})
@patch("src.cli.fetch_player_props")
def test_watch_mode_once_logic(mock_fetch, _mock_alert, tmp_path):
    from src.cli import watch_prop_arb
    mock_fetch.return_value = opp()
    result = watch_prop_arb("basketball_nba", bankroll=1000, min_roi=0.01, interval=1, once=True, db_path=str(tmp_path / "opps.db"))
    assert result["iterations"] == 1
    assert len(result["new_opportunities"]) == 1
    assert result["alerts_sent"] == 1
