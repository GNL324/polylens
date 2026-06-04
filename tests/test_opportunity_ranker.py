from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.analysis.opportunity_ranker import rank_opportunities


def _write_scan(tmp_path, opportunities):
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "scan.json"
    path.write_text(json.dumps({"top_candidates": opportunities}), encoding="utf-8")
    return path


def _opp(**overrides):
    base = {
        "id": "base",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "venue_pair": "kalshi_polymarket",
        "market_type": "crypto",
        "asset": "BTC",
        "estimated_edge": 0.03,
        "estimated_roi": 0.04,
        "estimated_profit": 8.0,
        "liquidity_score": 0.8,
        "execution_score": 0.8,
        "confidence_score": 0.8,
        "spread": 0.02,
        "hedge_available": True,
    }
    base.update(overrides)
    return base


def _rank(tmp_path, opportunities, **kwargs):
    _write_scan(tmp_path, opportunities)
    old = tmp_path.cwd() if hasattr(tmp_path, "cwd") else None
    import os

    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        return rank_opportunities(db_paths=(), scan_globs=("data/reports/*.json",), **kwargs)
    finally:
        os.chdir(previous)


def test_mocked_opportunities_from_all_venues_rank(tmp_path):
    result = _rank(
        tmp_path,
        [
            _opp(id="kalshi", venue_pair="kalshi_polymarket"),
            _opp(id="sportsbook", venue_pair="sportsbook_polymarket", market_type="prop", sport="NBA"),
            _opp(id="poly", venue_pair="polymarket_sportsbook", market_type="futures", sport="MLB"),
        ],
    )

    assert result["summary"]["status"] == "ranked"
    assert result["summary"]["returned"] == 3


def test_positive_ev_ranks_higher(tmp_path):
    result = _rank(tmp_path, [_opp(id="low", estimated_edge=0.01, estimated_roi=0.01), _opp(id="high", estimated_edge=0.08, estimated_roi=0.1)])

    assert result["ranked_opportunities"][0]["id"] == "high"


def test_stale_data_ranks_lower(tmp_path):
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    result = _rank(tmp_path, [_opp(id="fresh"), _opp(id="stale", timestamp=stale_time)])

    assert result["ranked_opportunities"][0]["id"] == "fresh"
    assert "stale data" in result["ranked_opportunities"][-1]["risk_notes"]


def test_low_liquidity_ranks_lower(tmp_path):
    result = _rank(tmp_path, [_opp(id="liquid", liquidity_score=0.9), _opp(id="thin", liquidity_score=0.05)])

    assert result["ranked_opportunities"][0]["id"] == "liquid"
    assert "low liquidity" in result["ranked_opportunities"][-1]["risk_notes"]


def test_high_ev_poor_execution_risk_ranks_lower(tmp_path):
    result = _rank(
        tmp_path,
        [
            _opp(id="balanced", estimated_edge=0.04, estimated_roi=0.05, execution_score=0.9, confidence_score=0.9, spread=0.02),
            _opp(id="risky", estimated_edge=0.08, estimated_roi=0.12, execution_score=0.05, confidence_score=0.2, spread=0.18, hedge_available=False),
        ],
    )

    assert result["ranked_opportunities"][0]["id"] == "balanced"
    assert "weak market matching" in result["ranked_opportunities"][-1]["risk_notes"]


def test_one_sided_directional_exposure_penalized(tmp_path):
    result = _rank(
        tmp_path,
        [
            _opp(id="hedged", venue_pair="kalshi_polymarket", hedge_available=True),
            _opp(id="onesided", venue_pair="kalshi", side="buy yes", strategy="directional", hedge_available=False),
        ],
    )

    assert result["ranked_opportunities"][0]["id"] == "hedged"
    assert "one-sided directional exposure" in result["ranked_opportunities"][-1]["risk_notes"]


def test_json_export_works(tmp_path):
    _write_scan(tmp_path, [_opp(id="export")])
    import os

    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = rank_opportunities(db_paths=(), scan_globs=("data/reports/*.json",), export=True)
    finally:
        os.chdir(previous)

    assert result["files"]["json"] == "data/reports/opportunity_rankings.json"
    assert result["files"]["csv"] == "data/reports/opportunity_rankings.csv"
    assert (tmp_path / "data" / "reports" / "opportunity_rankings.json").exists()
    assert (tmp_path / "data" / "reports" / "opportunity_rankings.csv").read_text(encoding="utf-8").startswith("rank,id")


def test_empty_data_returns_clean_no_opportunities(tmp_path):
    import os

    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = rank_opportunities(db_paths=(), scan_globs=("data/reports/*.json",))
    finally:
        os.chdir(previous)

    assert result["summary"]["status"] == "no_opportunities"
    assert result["ranked_opportunities"] == []
    assert "no local opportunities found" in result["summary"]["data_quality_warnings"][0]
