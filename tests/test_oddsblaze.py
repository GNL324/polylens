import json

import pytest

from src.adapters.oddsblaze import MissingOddsBlazeKey, OddsBlazeClient, normalize_oddsblaze_payload
from src.analysis.prop_arbitrage import scan_prop_arbitrage


def oddsblaze_payload(price_over=130, price_under=130):
    return {
        "updated": "2026-06-10T12:00:00Z",
        "league": "nba",
        "sportsbook": "draftkings",
        "events": [
            {
                "id": "evt",
                "start_time": "2026-06-10T23:00:00Z",
                "home_team": "New York Knicks",
                "away_team": "Boston Celtics",
                "odds": [
                    {
                        "id": "over",
                        "market": "Player Points",
                        "name": "Over 28.5",
                        "price": price_over,
                        "main": True,
                        "side": "Over",
                        "line": 28.5,
                        "player_name": "Jalen Brunson",
                        "links": {"desktop": "https://example.test/desktop", "mobile": "https://example.test/mobile"},
                    },
                    {
                        "id": "under",
                        "market": "Player Points",
                        "name": "Under 28.5",
                        "price": price_under,
                        "main": True,
                        "side": "Under",
                        "line": 28.5,
                        "player_name": "Jalen Brunson",
                    },
                ],
            }
        ],
    }


def real_shaped_oddsblaze_payload():
    return {
        "updated": "2026-06-10T12:00:00Z",
        "league": "nba",
        "sportsbook": {"id": "betmgm", "name": "BetMGM"},
        "events": [
            {
                "id": "evt",
                "start_time": "2026-06-10T23:00:00Z",
                "home_team": "New York Knicks",
                "away_team": "San Antonio Spurs",
                "odds": [
                    {
                        "id": "BetMGM#evt#Player Points + Rebounds#OG Anunoby Over 22.5",
                        "market": "Player Points + Rebounds",
                        "name": "OG Anunoby Over 22.5",
                        "price": "+100",
                        "main": True,
                        "selection": {"line": 22.5, "name": "OG Anunoby", "side": "Over"},
                        "player": {"id": "player-1", "name": "OG Anunoby"},
                    },
                    {
                        "id": "BetMGM#evt#Player Points + Rebounds#OG Anunoby Under 22.5",
                        "market": "Player Points + Rebounds",
                        "name": "OG Anunoby Under 22.5",
                        "price": "-130",
                        "main": True,
                        "selection": {"line": 22.5, "name": "OG Anunoby", "side": "Under"},
                        "player": {"id": "player-1", "name": "OG Anunoby"},
                    },
                ],
            }
        ],
    }


def test_oddsblaze_normalization():
    rows = normalize_oddsblaze_payload(oddsblaze_payload())
    assert len(rows) == 2
    assert rows[0]["provider"] == "oddsblaze"
    assert rows[0]["sportsbook"] == "draftkings"
    assert rows[0]["league"] == "NBA"
    assert rows[0]["event_id"] == "evt"
    assert rows[0]["player"] == "Jalen Brunson"
    assert rows[0]["market_type"] == "player_points"
    assert rows[0]["side"] == "over"
    assert rows[0]["line"] == 28.5
    assert rows[0]["american_price"] == 130
    assert rows[0]["deeplink_desktop"] == "https://example.test/desktop"
    assert "links" not in rows[0]["raw"]


def test_oddsblaze_real_shaped_selection_normalization():
    rows = normalize_oddsblaze_payload(real_shaped_oddsblaze_payload())
    assert [row["side"] for row in rows] == ["over", "under"]
    assert [row["line"] for row in rows] == [22.5, 22.5]
    assert rows[0]["sportsbook"] == "betmgm"
    assert rows[0]["bookmaker"] == "BetMGM"
    assert rows[0]["bookmaker_key"] == "betmgm"
    assert rows[0]["player"] == "OG Anunoby"
    assert rows[0]["market_type"] == "player_points_rebounds"
    assert rows[0]["prop_period"] == "full_game"


def test_oddsblaze_combo_market_normalization():
    payload = oddsblaze_payload()
    payload["events"][0]["odds"][0]["market"] = "Player Points + Rebounds + Assists"
    rows = normalize_oddsblaze_payload(payload)
    assert rows[0]["market_type"] == "player_points_rebounds_assists"


def test_oddsblaze_period_normalization():
    payload = oddsblaze_payload()
    payload["events"][0]["odds"] = [
        {**payload["events"][0]["odds"][0], "market": "1st Quarter Player Points", "id": "q1-points"},
        {**payload["events"][0]["odds"][0], "market": "1st Half Player Assists", "id": "h1-assists"},
        {**payload["events"][0]["odds"][0], "market": "2nd Half Player Rebounds", "id": "h2-rebounds"},
        {**payload["events"][0]["odds"][0], "market": "Player Points", "id": "full-points"},
    ]
    rows = normalize_oddsblaze_payload(payload)
    assert [row["prop_period"] for row in rows] == ["first_quarter", "first_half", "second_half", "full_game"]


def test_oddsblaze_does_not_collapse_combo_prop_identities():
    payload = oddsblaze_payload()
    markets = [
        ("Player Blocks", "player_blocks"),
        ("Player Blocks + Steals", "player_blocks_steals"),
        ("Player Assists", "player_assists"),
        ("Player Assists + Rebounds", "player_rebounds_assists"),
        ("Player Points", "player_points"),
        ("Player Points + Assists", "player_points_assists"),
    ]
    payload["events"][0]["odds"] = [
        {**payload["events"][0]["odds"][0], "market": market, "id": market}
        for market, _expected in markets
    ]
    rows = normalize_oddsblaze_payload(payload)
    assert [row["prop_identity"] for row in rows] == [expected for _market, expected in markets]


def test_oddsblaze_missing_key_fails_clearly(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDSBLAZE_API_KEY", raising=False)
    client = OddsBlazeClient(raw_dir=tmp_path)
    with pytest.raises(MissingOddsBlazeKey, match="ODDSBLAZE_API_KEY"):
        client.fetch_odds("draftkings", "nba")


def test_oddsblaze_cli_smoke_json(monkeypatch, capsys):
    from src import cli

    monkeypatch.setattr(cli.OddsBlazeClient, "fetch_odds", lambda self, **kwargs: normalize_oddsblaze_payload(oddsblaze_payload()))
    rows = cli.fetch_oddsblaze_odds("draftkings", "nba", market_contains="Player", main=True, live=False, as_json=True)
    captured = capsys.readouterr().out
    assert rows[0]["source"] == "oddsblaze"
    assert json.loads(captured)[0]["sportsbook"] == "draftkings"


def test_scan_prop_arb_with_mocked_oddsblaze():
    rows = normalize_oddsblaze_payload(oddsblaze_payload())
    result = scan_prop_arbitrage(rows)
    assert len(result["prop_arbitrage_candidates"]) == 1
    arb = result["prop_arbitrage_candidates"][0]
    assert arb["over_source"] == "oddsblaze"
    assert arb["under_source"] == "oddsblaze"
    assert arb["guaranteed_roi"] > 0


def test_prop_arb_side_diagnostics_for_invalid_rows():
    rows = normalize_oddsblaze_payload(real_shaped_oddsblaze_payload())
    rows.append({**rows[0], "side": None, "raw": {"market": "Player Double Double", "name": "OG Anunoby Yes", "selection": {"name": "OG Anunoby", "side": "Yes"}}})
    result = scan_prop_arbitrage(rows)
    assert result["side_counts_by_provider_sportsbook"] == [{"provider": "oddsblaze", "sportsbook": "betmgm", "over": 1, "under": 1, "invalid": 1}]
    assert result["invalid_side_examples"][0]["selection"]["side"] == "Yes"


def test_prop_arb_rejects_blocks_vs_blocks_steals_market_mismatch():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "OG Anunoby", "market": "Player Blocks", "market_type": "player_blocks", "prop_identity": "player_blocks", "line": 1.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "OG Anunoby", "market": "Player Blocks + Steals", "market_type": "player_blocks_steals", "prop_identity": "player_blocks_steals", "line": 1.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.45},
    ]
    result = scan_prop_arbitrage(props)
    assert result["prop_arbitrage_candidates"] == []
    assert result["market_mismatch_count"] == 1
    assert result["market_mismatch_examples"][0]["rejection_reason"] == "market mismatch"


def test_prop_arb_rejects_assists_vs_assists_rebounds_market_mismatch():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market": "Player Assists", "market_type": "player_assists", "prop_identity": "player_assists", "line": 6.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market": "Player Assists + Rebounds", "market_type": "player_rebounds_assists", "prop_identity": "player_rebounds_assists", "line": 6.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.45},
    ]
    result = scan_prop_arbitrage(props)
    assert result["prop_arbitrage_candidates"] == []
    assert result["market_mismatch_count"] == 1


def test_prop_arb_rejects_points_vs_points_assists_market_mismatch():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market": "Player Points", "market_type": "player_points", "prop_identity": "player_points", "line": 28.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market": "Player Points + Assists", "market_type": "player_points_assists", "prop_identity": "player_points_assists", "line": 28.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.45},
    ]
    result = scan_prop_arbitrage(props)
    assert result["prop_arbitrage_candidates"] == []
    assert result["market_mismatch_count"] == 1


def test_prop_arb_rejects_first_quarter_assists_vs_full_game_assists():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market": "1st Quarter Player Assists", "market_type": "player_assists", "prop_identity": "player_assists", "prop_period": "first_quarter", "line": 1.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Josh Hart", "market": "Player Assists", "market_type": "player_assists", "prop_identity": "player_assists", "prop_period": "full_game", "line": 1.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.45},
    ]
    result = scan_prop_arbitrage(props)
    assert result["prop_arbitrage_candidates"] == []
    assert result["period_mismatch_count"] == 1
    assert result["period_mismatch_examples"][0]["rejection_reason"] == "period mismatch"


def test_prop_arb_rejects_first_quarter_points_vs_full_game_points():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market": "1st Quarter Player Points", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "first_quarter", "line": 7.5, "side": "over", "bookmaker": "BookA", "implied_probability": 0.45},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market": "Player Points", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 7.5, "side": "under", "bookmaker": "BookB", "implied_probability": 0.45},
    ]
    result = scan_prop_arbitrage(props)
    assert result["prop_arbitrage_candidates"] == []
    assert result["period_mismatch_count"] == 1


def test_prop_arb_rejects_stale_leg():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 28.5, "side": "over", "bookmaker": "BookA", "odds": 130, "implied_probability": 0.4348, "last_update": "2026-06-10T17:45:00Z"},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 28.5, "side": "under", "bookmaker": "BookB", "odds": 130, "implied_probability": 0.4348, "last_update": "2026-06-10T17:52:00Z"},
    ]
    result = scan_prop_arbitrage(props, max_leg_age_seconds=180, max_cross_leg_update_gap_seconds=300, scan_time="2026-06-10T17:52:00Z")
    assert result["prop_arbitrage_candidates"] == []
    assert result["stale_leg_count"] == 1
    assert result["stale_leg_examples"][0]["stale_sides"] == ["over"]
    assert result["rejection_reasons"]["stale leg"] == 1


def test_prop_arb_rejects_cross_leg_update_gap():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 28.5, "side": "over", "bookmaker": "BookA", "odds": 130, "implied_probability": 0.4348, "last_update": "2026-06-10T17:48:00Z"},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 28.5, "side": "under", "bookmaker": "BookB", "odds": 130, "implied_probability": 0.4348, "last_update": "2026-06-10T17:52:30Z"},
    ]
    result = scan_prop_arbitrage(props, max_leg_age_seconds=600, max_cross_leg_update_gap_seconds=180, scan_time="2026-06-10T17:52:30Z")
    assert result["prop_arbitrage_candidates"] == []
    assert result["stale_leg_count"] == 0
    assert result["update_gap_rejection_count"] == 1
    assert result["rejection_reasons"]["cross-leg update gap too large"] == 1


def test_prop_arb_accepts_fresh_legs_with_small_update_gap():
    props = [
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 28.5, "side": "over", "bookmaker": "BookA", "odds": 130, "implied_probability": 0.4348, "last_update": "2026-06-10T17:51:00Z"},
        {"sport": "basketball_nba", "league": "NBA", "event_id": "evt", "player": "Jalen Brunson", "market_type": "player_points", "prop_identity": "player_points", "prop_period": "full_game", "line": 28.5, "side": "under", "bookmaker": "BookB", "odds": 130, "implied_probability": 0.4348, "last_update": "2026-06-10T17:52:00Z"},
    ]
    result = scan_prop_arbitrage(props, max_leg_age_seconds=180, max_cross_leg_update_gap_seconds=180, scan_time="2026-06-10T17:52:00Z")
    assert len(result["prop_arbitrage_candidates"]) == 1
    assert result["stale_leg_count"] == 0
    assert result["update_gap_rejection_count"] == 0


def test_scan_prop_arb_cli_uses_oddsblaze_provider(monkeypatch):
    from src import cli

    monkeypatch.setattr(cli, "fetch_oddsblaze_odds", lambda **kwargs: normalize_oddsblaze_payload(oddsblaze_payload()))
    result = cli.scan_prop_arb("basketball_nba", provider="oddsblaze", oddsblaze_sportsbooks=["draftkings"])
    assert result["provider"] == "oddsblaze"
    assert len(result["prop_arbitrage_candidates"]) == 1


def test_scan_prop_arb_oddsblaze_internal_market_uses_player_filter(monkeypatch):
    from src import cli

    calls = []

    def fake_fetch_oddsblaze_odds(**kwargs):
        calls.append(kwargs)
        return normalize_oddsblaze_payload(oddsblaze_payload())

    monkeypatch.setattr(cli, "fetch_oddsblaze_odds", fake_fetch_oddsblaze_odds)
    result = cli.scan_prop_arb(
        "basketball_nba",
        provider="oddsblaze",
        oddsblaze_sportsbooks=["draftkings"],
        markets="player_points",
    )
    assert calls[0]["market_contains"] == "Player"
    assert result["normalized_props"] == 2
    assert len(result["prop_arbitrage_candidates"]) == 1


def test_scan_prop_arb_oddsblaze_market_contains_override(monkeypatch):
    from src import cli

    calls = []

    monkeypatch.setattr(
        cli,
        "fetch_oddsblaze_odds",
        lambda **kwargs: calls.append(kwargs) or normalize_oddsblaze_payload(oddsblaze_payload()),
    )
    cli.scan_prop_arb(
        "basketball_nba",
        provider="oddsblaze",
        oddsblaze_sportsbooks=["draftkings"],
        markets="player_points",
        oddsblaze_market_contains="Points",
    )
    assert calls[0]["market_contains"] == "Points"
