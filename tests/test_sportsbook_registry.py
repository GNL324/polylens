from __future__ import annotations

from src.web.dashboard import (
    book_pair_links_html,
    format_bet_instructions,
    profile_performance_rows,
    prop_arbitrage_rows,
    sportsbook_links_html,
)
from src.web.sportsbook_registry import (
    normalize_provider_sportsbook_key,
    normalize_sportsbook_key,
    sportsbook_catalog,
    sportsbook_display_name,
    sportsbook_url,
)
from src.storage.opportunities import create_scanner_profile, get_scanner_profile, save_prop_arbitrage_scan_result


def test_sportsbook_registry_lookups() -> None:
    catalog = {row["key"]: row["label"] for row in sportsbook_catalog()}

    assert catalog["betmgm"] == "BetMGM"
    assert catalog["betrivers"] == "BetRivers"
    assert sportsbook_display_name("espnbet") == "ESPN Bet"
    assert sportsbook_url("betmgm", "basketball_nba") == "https://www.ny.betmgm.com/en/sports/basketball-7"


def test_provider_key_translation_and_future_normalization() -> None:
    assert normalize_provider_sportsbook_key("odds_api", "betrivers") == "betrivers"
    assert normalize_provider_sportsbook_key("oddsblaze", "BetRivers NY") == "betrivers"
    assert normalize_provider_sportsbook_key("oddsblaze", "BetMGM NY") == "betmgm"
    assert normalize_sportsbook_key("FanDuel") == "fanduel"


def test_missing_sportsbook_url_handling() -> None:
    assert sportsbook_url("unknownbook", "basketball_nba") == ""
    assert sportsbook_url("betmgm", "baseball_mlb") == "https://www.ny.betmgm.com/en/sports"


def test_clickable_sportsbook_badge_rendering() -> None:
    html = sportsbook_links_html(["fanduel", "bovada"], sport="basketball_nba")

    assert 'href="https://sportsbook.fanduel.com/navigation/nba"' in html
    assert 'target="_blank"' in html
    assert "FanDuel" in html
    assert "Bovada" in html
    assert sportsbook_links_html([], sport="basketball_nba") == "All sportsbooks"


def test_opportunity_detail_sportsbook_links_and_copy_format() -> None:
    opportunity = {
        "player": "Mitchell Robinson",
        "prop_type": "player_points",
        "line": 2.5,
        "over_book": "BetRivers",
        "over_odds": -139,
        "stake_over": 610.92,
        "under_book": "Bovada",
        "under_odds": 170,
        "stake_under": 389.08,
        "guaranteed_profit_amount": 50.42,
        "guaranteed_roi": 0.0504,
    }

    text = format_bet_instructions(opportunity)

    assert "Mitchell Robinson Over 2.5 Points" in text
    assert "Book: BetRivers" in text
    assert "Odds: -139" in text
    assert "Stake: $610.92" in text
    assert "Mitchell Robinson Under 2.5 Points" in text
    assert "Book: Bovada" in text
    assert "Odds: +170" in text
    assert "Expected Profit: $50.42" in text
    assert "ROI: 5.04%" in text


def test_live_opportunity_rows_include_sportsbook_links(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(db_path=str(db_path), name="NBA", sport="basketball_nba", markets=["player_points"], is_active=True)
    profile = get_scanner_profile(profile_id, db_path=str(db_path))
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 2,
            "matched_prop_pairs": 1,
            "rejected_pairs": 0,
            "scan_duration_seconds": 0.1,
            "prop_arbitrage_candidates": [
                {
                    "player": "Player A",
                    "prop_type": "player_points",
                    "line": 1.5,
                    "over_book": "BetMGM",
                    "over_odds": 155,
                    "under_book": "BetRivers",
                    "under_odds": -121,
                    "guaranteed_roi": 0.05,
                    "guaranteed_profit_amount": 50,
                    "ranking_score": 50,
                }
            ],
            "rejection_reasons": {},
        },
        db_path=str(db_path),
        sport="basketball_nba",
        markets="player_points",
        profile_id=profile_id,
        profile_name="NBA",
    )

    row = prop_arbitrage_rows(db_path, active_profile=profile)[0]

    assert row["over_book_link"] == "BetMGM ↗"
    assert row["under_book_link"] == "BetRivers ↗"
    assert row["over_book_url"] == "https://www.ny.betmgm.com/en/sports/basketball-7"
    assert row["under_book_url"] == "https://ny.betrivers.com/?page=sportsbook&group=1000093652&type=matches"


def test_profile_performance_and_book_pair_links(tmp_path) -> None:
    db_path = tmp_path / "opportunities.db"
    profile_id = create_scanner_profile(
        db_path=str(db_path),
        name="NBA",
        sport="basketball_nba",
        markets=["player_points"],
        sportsbooks=["betmgm", "betrivers"],
        is_active=True,
    )
    save_prop_arbitrage_scan_result(
        {
            "props_fetched": 2,
            "matched_prop_pairs": 1,
            "rejected_pairs": 0,
            "scan_duration_seconds": 0.1,
            "prop_arbitrage_candidates": [
                {
                    "player": "Player A",
                    "prop_type": "player_points",
                    "line": 1.5,
                    "over_book": "BetMGM",
                    "over_odds": 155,
                    "under_book": "BetRivers",
                    "under_odds": -121,
                    "guaranteed_roi": 0.05,
                    "guaranteed_profit_amount": 50,
                    "ranking_score": 50,
                }
            ],
            "rejection_reasons": {},
        },
        db_path=str(db_path),
        sport="basketball_nba",
        markets="player_points",
        profile_id=profile_id,
        profile_name="NBA",
    )

    profile_row = profile_performance_rows(str(db_path))[0]
    pair_html = book_pair_links_html("betmgm <-> betrivers", sport="basketball_nba")

    assert "BetMGM" in profile_row["sportsbooks_html"]
    assert "BetRivers" in profile_row["sportsbooks_html"]
    assert "https://www.ny.betmgm.com/en/sports/basketball-7" in profile_row["sportsbooks_html"]
    assert "BetMGM" in pair_html
    assert "BetRivers" in pair_html
