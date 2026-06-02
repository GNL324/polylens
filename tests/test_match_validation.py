from src.analysis.cross_market import compare_wallet_markets_to_kalshi
from src.analysis.match_diagnostics import explain_market_matches
from src.analysis.match_validation import classify_kalshi_product, validate_market_type_compatibility
from src.analysis.sports_parser import parse_market_record
from src.analysis.structured_matching import score_structured_sports_pair


KNICKS = {"conditionId": "pm-knicks", "title": "Will the New York Knicks win the 2026 NBA Finals?", "slug": "knicks-2026-nba-finals"}


def multileg_kalshi():
    return {
        "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026ABC-KNICKS",
        "event_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-S2026ABC",
        "title": "yes Tampa Bay,yes Washington,yes New York Y,yes Over 7.5 runs scored,yes New York Knicks",
        "subtitle": "Associated Markets: many legs including Knicks",
        "custom_strike": {"Associated Markets": "A,B,C", "Associated Events": "X,Y,Z"},
    }


def cross_category_kalshi():
    return {
        "ticker": "KXMVECROSSCATEGORY-S2026ABC-KNICKS",
        "event_ticker": "KXMVECROSSCATEGORY-S2026ABC",
        "title": "yes New York Knicks,yes Bitcoin above $100k,yes CPI under 3%",
        "subtitle": "cross category bundle",
    }


def outright_kalshi():
    return {"ticker": "KXNBAFINALS-26-KNICKS", "title": "Will the New York Knicks win the 2026 NBA Finals?"}


def player_prop_kalshi():
    return {"ticker": "KXNBAPLAYER-KNICKS-BRUNSONPTS", "title": "Will Jalen Brunson score over 30.5 points?"}


def test_nba_finals_vs_multileg_rejection():
    pm = parse_market_record(KNICKS, "polymarket")
    kalshi_market = multileg_kalshi()
    kalshi = parse_market_record(kalshi_market, "kalshi")
    assert classify_kalshi_product(kalshi_market, kalshi) == "multileg"
    assert score_structured_sports_pair(KNICKS, pm, kalshi_market, kalshi) is None
    assert compare_wallet_markets_to_kalshi([KNICKS], [kalshi_market]) == []


def test_nba_finals_vs_cross_category_rejection():
    validation = validate_market_type_compatibility(KNICKS, cross_category_kalshi())
    assert validation.accepted is False
    assert validation.rejection_reason == "incompatible market types"
    assert compare_wallet_markets_to_kalshi([KNICKS], [cross_category_kalshi()]) == []


def test_outright_vs_player_prop_rejection():
    validation = validate_market_type_compatibility(KNICKS, player_prop_kalshi())
    assert validation.accepted is False
    assert validation.polymarket_product_type == "championship_winner"
    assert validation.kalshi_product_type == "player_prop"


def test_outright_vs_outright_acceptance():
    validation = validate_market_type_compatibility(KNICKS, outright_kalshi())
    assert validation.accepted is True
    candidates = compare_wallet_markets_to_kalshi([KNICKS], [outright_kalshi()])
    assert len(candidates) == 1
    assert candidates[0]["market_type_validation"]["accepted"] is True


def test_diagnostics_populated_correctly():
    diagnostics = explain_market_matches([KNICKS], [multileg_kalshi()], max_candidates=5)
    reasons = {item["reason"] for item in diagnostics["top_rejected_candidate_reasons"]}
    assert "incompatible market types" in reasons
    rejected = [item for item in diagnostics["diagnostics"] if item.get("rejection_reason") == "incompatible market types"]
    assert rejected
    assert rejected[0]["market_type_validation"]["kalshi_product_type"] == "multileg"
