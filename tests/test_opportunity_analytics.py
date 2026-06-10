from src.storage.opportunity_analytics import (
    init_opportunity_analytics_db,
    mark_inactive,
    opportunity_leaderboard,
    opportunity_lifetimes,
    opportunity_quality_report,
    record_scan_analytics,
    stable_opportunity_id,
)
import json
import sqlite3


def candidate(player="Jalen Brunson", roi=0.08, over_book="BookA", under_book="BookB"):
    return {
        "player": player,
        "prop_type": "player_points",
        "prop_identity": "player_points",
        "prop_period": "full_game",
        "line": 28.5,
        "over_book": over_book,
        "under_book": under_book,
        "over_odds": 130,
        "under_odds": 130,
        "implied_probability_sum": 0.8696,
        "guaranteed_roi": roi,
        "over_source": "oddsblaze",
        "under_source": "oddsblaze",
    }


def scan_result(final=None, pre=None, diagnostics=None):
    return {
        "pre_filter_candidates": pre if pre is not None else list(final or []),
        "prop_arbitrage_candidates": final or [],
        "diagnostics": diagnostics or [],
    }


def test_stable_opportunity_ids_ignore_roi_and_odds():
    left = candidate(roi=0.05)
    right = {**left, "guaranteed_roi": 0.12, "over_odds": 125}
    assert stable_opportunity_id(left) == stable_opportunity_id(right)
    assert stable_opportunity_id(left) != stable_opportunity_id({**left, "under_book": "BookC"})


def test_lifecycle_updates_seen_count_and_roi_bounds(tmp_path):
    db_path = str(tmp_path / "opps.db")
    first = candidate(roi=0.05)
    second = candidate(roi=0.12)
    record_scan_analytics(scan_result(final=[first]), db_path=db_path, observed_at="2026-06-10T17:00:00Z")
    record_scan_analytics(scan_result(final=[second]), db_path=db_path, observed_at="2026-06-10T17:02:00Z")

    leaderboard = opportunity_leaderboard(db_path=db_path)
    row = leaderboard["players"][0]
    assert row["player"] == "Jalen Brunson"
    assert row["appearances"] == 2
    assert row["max_roi"] == 0.12

    lifetimes = opportunity_lifetimes(db_path=db_path)
    assert lifetimes["average_duration_seconds"] == 120.0
    assert lifetimes["longest_lived"][0]["seen_count"] == 2
    assert lifetimes["longest_lived"][0]["latest_roi"] == 0.12


def test_expiration_logic_marks_inactive(tmp_path):
    db_path = str(tmp_path / "opps.db")
    record_scan_analytics(scan_result(final=[candidate()]), db_path=db_path, observed_at="2026-06-10T17:00:00Z")
    expired = mark_inactive(db_path, observed_at="2026-06-10T17:20:00Z", inactive_after_seconds=600)
    assert expired == 1
    assert opportunity_lifetimes(db_path=db_path)["longest_lived"][0]["status"] == "inactive"


def test_leaderboard_aggregation_by_pair_provider_prop_player(tmp_path):
    db_path = str(tmp_path / "opps.db")
    rows = [
        candidate(player="Jalen Brunson", roi=0.05, over_book="BookA", under_book="BookB"),
        candidate(player="Josh Hart", roi=0.07, over_book="BookA", under_book="BookB"),
        candidate(player="Jalen Brunson", roi=0.09, over_book="BookC", under_book="BookD"),
    ]
    record_scan_analytics(scan_result(final=rows), db_path=db_path, observed_at="2026-06-10T17:00:00Z")
    result = opportunity_leaderboard(db_path=db_path)
    assert result["sportsbook_pairs"][0]["sportsbook_pair"] == "BookA / BookB"
    assert result["sportsbook_pairs"][0]["appearances"] == 2
    assert result["providers"][0]["provider"] == "oddsblaze"
    assert result["prop_types"][0]["prop_identity"] == "player_points"
    assert result["players"][0]["player"] == "Jalen Brunson"


def test_quality_report_from_rejected_snapshots(tmp_path):
    db_path = str(tmp_path / "opps.db")
    rejected = [
        {**candidate(), "rejection_reason": "stale leg"},
        {**candidate(), "rejection_reason": "market mismatch"},
        {**candidate(), "rejection_reason": "invalid side"},
    ]
    record_scan_analytics(scan_result(final=[], pre=[], diagnostics=rejected), db_path=db_path, observed_at="2026-06-10T17:00:00Z")
    report = opportunity_quality_report(db_path=db_path)
    assert report["total_rejections"] == 3
    assert report["stale_leg_rejection_percent"] == 33.3333
    assert report["market_mismatch_percent"] == 33.3333
    assert report["invalid_side_percent"] == 33.3333


def test_leaderboard_backfills_historical_prop_opportunities(tmp_path):
    db_path = str(tmp_path / "opps.db")
    init_opportunity_analytics_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE prop_arbitrage_opportunities (
                id INTEGER PRIMARY KEY,
                discovered_at TEXT,
                scan_run_id INTEGER,
                player TEXT,
                prop_type TEXT,
                line REAL,
                over_book TEXT,
                over_odds INTEGER,
                under_book TEXT,
                under_odds INTEGER,
                implied_probability_sum REAL,
                guaranteed_roi REAL,
                raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO prop_arbitrage_opportunities
            (discovered_at, scan_run_id, player, prop_type, line, over_book, over_odds, under_book, under_odds,
             implied_probability_sum, guaranteed_roi, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-10T17:00:00Z",
                1,
                "Jalen Brunson",
                "player_points",
                28.5,
                "BookA",
                130,
                "BookB",
                130,
                0.8696,
                0.15,
                json.dumps({"prop_period": "full_game", "over_source": "legacy"}),
            ),
        )
    result = opportunity_leaderboard(db_path=db_path)
    assert result["players"][0]["player"] == "Jalen Brunson"
    assert result["players"][0]["appearances"] == 1
