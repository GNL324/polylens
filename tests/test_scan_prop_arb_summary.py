from src.cli import scan_prop_arb_summary


def test_scan_prop_arb_summary_omits_candidate_payloads():
    result = {
        "props_fetched": 120,
        "normalized_props": 120,
        "matched_prop_pairs": 8,
        "prop_arbitrage_candidates": [{"player": "Test", "guaranteed_roi": 0.02}],
        "rejection_reasons": {"stale leg": 3},
        "provider": "all",
        "scan_duration_seconds": 1.2,
        "analytics": {"snapshots_recorded": 4, "final_observed": 1},
    }
    summary = scan_prop_arb_summary(result)
    assert summary["true_arb_candidates"] == 1
    assert summary["props_fetched"] == 120
    assert "prop_arbitrage_candidates" not in summary
    assert summary["analytics"]["snapshots_recorded"] == 4
