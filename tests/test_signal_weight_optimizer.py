from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.analysis.opportunity_scoring_v3 import ScoringV3Weights
from src.analysis.outcome_validator import validate_completed_markets
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.analysis.signal_weight_optimizer import (
    V3_COMPONENT_NAMES,
    WeightOptimizerConfig,
    blend_weights,
    extract_component_features,
    load_validation_samples,
    normalize_weights,
    optimize_signal_weights,
    pearson_correlation,
    weights_as_dict,
)
from src.analysis.signal_weight_report import (
    build_signal_weight_report,
    export_signal_weight_recommendation,
    format_weight_optimization_cli,
    format_weight_optimization_telegram,
    mission_control_section,
)
from src.cli import signal_weight_optimize_cli, signal_weight_report_cli
from src.integrations.telegram_console import TelegramConsole, TelegramConsoleConfig
from src.web.mission_control_data import mission_control_snapshot


NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
WEIGHTS = ScoringV3Weights(
    wallet_performance=0.10,
    wallet_consistency=0.08,
    wallet_momentum=0.07,
    signal_family_accuracy=0.10,
    market_liquidity=0.10,
    bid_ask_spread=0.08,
    time_to_expiry=0.07,
    validating_wallets=0.06,
    consensus_strength=0.05,
    contrarian_bonus=0.04,
    similar_market_edge=0.10,
    execution_quality_penalty=0.08,
    portfolio_exposure_penalty=0.07,
)
CONFIG = WeightOptimizerConfig(min_weight=0.02, max_weight=0.25, min_samples=3, full_confidence_samples=20, max_swing=0.35)


def _scoring_v3_raw(**component_scores: float) -> str:
    components = {
        name: {"score": component_scores.get(name, 50.0), "weight": 0.08, "weighted_contribution": 4.0, "reason": "test"}
        for name in V3_COMPONENT_NAMES
    }
    payload = {"scoring_v3": {"final_score": 72.0, "components": components}, "confidence": 0.75}
    return json.dumps({"source": "test", "raw": payload})


def _seed_weight_db(db_path) -> None:
    init_paper_trading_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_runs (id, run_timestamp, opportunities_seen, orders_created, positions_opened, positions_settled, equity, raw_json)
            VALUES (1, '2026-06-25T09:00:00Z', 6, 6, 6, 0, 100, '{}')
            """
        )
        rows = [
            (0.85, 1.0, 80.0, 90.0, 30.0),
            (0.75, 0.0, 20.0, 25.0, 40.0),
            (0.65, 1.0, 85.0, 88.0, 50.0),
            (0.55, 0.0, 25.0, 30.0, 60.0),
            (0.45, 1.0, 88.0, 92.0, 70.0),
            (0.35, 0.0, 15.0, 18.0, 80.0),
        ]
        for index, (confidence, settlement, wallet_perf, liquidity, penalty) in enumerate(rows, start=1):
            raw_json = _scoring_v3_raw(
                wallet_performance=wallet_perf,
                wallet_consistency=wallet_perf - 5,
                signal_family_accuracy=wallet_perf,
                market_liquidity=liquidity,
                execution_quality_penalty=penalty,
                portfolio_exposure_penalty=10.0,
            )
            conn.execute(
                """
                INSERT INTO paper_orders
                (id, run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json, created_timestamp)
                VALUES (?, 1, ?, 'copy_alpha', 'yes', ?, ?, 'POLY', 0.50, 10, 'filled', ?, '2026-06-25T09:00:00Z')
                """,
                (index, f"opp-{index}", f"market-{index}", f"Market {index}", raw_json),
            )
            conn.execute(
                """
                INSERT INTO paper_positions
                (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side,
                 entry_timestamp, entry_price, shares, notional, status, current_price, exit_timestamp,
                 exit_price, realized_pnl, unrealized_pnl, roi, signal_family, source_wallet, confidence,
                 validation_score, market_category, market_liquidity, time_to_expiry_seconds)
                VALUES (?, ?, ?, 'copy_alpha', ?, ?, 'POLY', 'yes',
                    '2026-06-25T09:00:00Z', 0.50, 20, 10, 'closed', ?, '2026-06-25T10:00:00Z',
                    ?, ?, 0, ?, 'family_a', ?, ?, 0.4, 'politics', ?, 3600)
                """,
                (
                    index,
                    index,
                    f"opp-{index}",
                    f"market-{index}",
                    f"Market {index}",
                    settlement,
                    settlement,
                    round((settlement - 0.5) * 20, 6),
                    round(((settlement - 0.5) * 20) / 10, 6),
                    f"0xwallet{index}",
                    confidence,
                    liquidity,
                ),
            )
            conn.execute(
                """
                INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason)
                VALUES (1, ?, '2026-06-25T10:00:00Z', ?, ?, 0, 'test')
                """,
                (index, settlement, round((settlement - 0.5) * 20, 6)),
            )


def test_normalize_weights_sum_to_one():
    raw = {name: float(index + 1) for index, name in enumerate(V3_COMPONENT_NAMES)}
    normalized = normalize_weights(raw, config=CONFIG)

    assert pytest.approx(sum(normalized.values()), rel=1e-6) == 1.0
    assert all(CONFIG.min_weight <= value <= CONFIG.max_weight for value in normalized.values())


def test_bounded_weights_respect_limits():
    raw = {name: 0.08 for name in V3_COMPONENT_NAMES}
    raw["wallet_performance"] = 0.20
    normalized = normalize_weights(raw, config=CONFIG)

    assert all(value >= CONFIG.min_weight for value in normalized.values())
    assert pytest.approx(sum(normalized.values()), rel=1e-6) == 1.0


def test_pearson_correlation_deterministic():
    xs = [10.0, 20.0, 30.0, 40.0]
    ys = [1.0, 0.0, 1.0, 0.0]

    assert pearson_correlation(xs, ys) == pearson_correlation(xs, ys)
    assert pearson_correlation(xs, xs) == pytest.approx(1.0)


def test_identical_input_produces_identical_output():
    samples = [
        {
            "validation_status": "correct",
            "correctness_score": 1.0,
            "confidence_at_entry": 0.8,
            "opportunity_score": 80,
            "market_liquidity": 1000,
            "execution_quality": 90,
            "time_to_expiry_seconds": 3600,
            "signal_family": "conviction",
            "raw_json": _scoring_v3_raw(wallet_performance=90),
        }
        for _ in range(6)
    ]

    first = optimize_signal_weights(samples, current=WEIGHTS, config=CONFIG)
    second = optimize_signal_weights(samples, current=WEIGHTS, config=CONFIG)

    assert first["suggested_weights"] == second["suggested_weights"]
    assert first["confidence"] == second["confidence"]


def test_small_sample_returns_current_weights():
    samples = [{"validation_status": "correct", "correctness_score": 1.0}] * 2
    result = optimize_signal_weights(samples, current=WEIGHTS, config=CONFIG)

    assert result["confidence"] == 0.0
    assert result["suggested_weights"] == weights_as_dict(WEIGHTS)
    assert "insufficient sample size" in result["rationale"]


def test_optimizer_stability_with_validation_db(tmp_path):
    db_path = tmp_path / "paper.db"
    _seed_weight_db(db_path)
    validate_completed_markets(db_path)

    samples = load_validation_samples(db_path, window_days=90, now=NOW)
    result = optimize_signal_weights(samples, current=WEIGHTS, config=CONFIG)

    assert result["sample_size"] >= CONFIG.min_samples
    assert 0.0 <= result["confidence"] <= 1.0
    assert pytest.approx(sum(result["suggested_weights"].values()), rel=1e-5) == 1.0
    assert result["auto_apply"] is False


def test_extract_component_features_from_scoring_v3():
    row = {"raw_json": _scoring_v3_raw(wallet_performance=77.0, market_liquidity=66.0)}
    features = extract_component_features(row)

    assert features["wallet_performance"] == 77.0
    assert features["market_liquidity"] == 66.0


def test_blend_weights_limits_swing():
    current = weights_as_dict(WEIGHTS)
    target = {name: (0.25 if name == "wallet_performance" else 0.02) for name in V3_COMPONENT_NAMES}
    target = normalize_weights(target, config=CONFIG)
    blended = blend_weights(current, target, blend_factor=0.35, config=CONFIG)

    assert pytest.approx(sum(blended.values()), rel=1e-5) == 1.0
    assert blended["wallet_performance"] != current["wallet_performance"]


def test_report_generation_and_export(tmp_path):
    db_path = tmp_path / "paper.db"
    _seed_weight_db(db_path)
    validate_completed_markets(db_path)

    report = build_signal_weight_report(db_path, window_days=90, current=WEIGHTS, config=CONFIG, now=NOW)
    export_path = export_signal_weight_recommendation(report, tmp_path / "signal_weight_recommendation.json")

    assert export_path.exists()
    assert report["auto_apply"] is False
    assert report["sample_size"] >= CONFIG.min_samples
    assert len(report["recommendations"]) == len(V3_COMPONENT_NAMES)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["auto_apply"] is False
    assert payload["sample_size"] == report["sample_size"]


def test_mission_control_signal_weight_section(tmp_path):
    db_path = tmp_path / "paper.db"
    _seed_weight_db(db_path)
    validate_completed_markets(db_path)

    snapshot = mission_control_snapshot(portfolio_db_path=db_path, now=NOW)
    section = snapshot["signal_weight_optimization"]

    assert section["auto_apply"] is False
    assert section["validation_sample_size"] >= CONFIG.min_samples
    assert "current_weights" in section
    assert "suggested_weights" in section
    assert section["confidence"] >= 0.0


def test_telegram_weight_optimization_rendering(tmp_path):
    db_path = tmp_path / "paper.db"
    _seed_weight_db(db_path)
    validate_completed_markets(db_path)
    report = build_signal_weight_report(db_path, now=NOW)

    text = format_weight_optimization_telegram(report)
    assert "⚖️ Weight Optimization" in text
    assert "Strongest factors" in text
    assert "not auto-applied" in text.lower()

    console = TelegramConsole(
        TelegramConsoleConfig(bot_token="token", admin_user_ids=frozenset({1}), audit_db_path=str(tmp_path / "audit.db"), paper_db_path=str(db_path)),
        health_provider=lambda: {"status": "healthy"},
        signals_provider=lambda: {},
        paper_provider=lambda: {"status": "healthy"},
        paper_intelligence_provider=lambda: {},
        risk_provider=lambda: {"halted": False},
        now_provider=lambda: NOW,
    )
    response = console.handle_text(1, "/weight_optimization")
    assert "⚖️ Weight Optimization" in response.text

    nav = console.handle_callback(1, "weight_optimization")
    assert "Weight Optimization" in nav.text
    assert "Strongest factors" in nav.text


def test_cli_signal_weight_commands(tmp_path, capsys):
    db_path = tmp_path / "paper.db"
    _seed_weight_db(db_path)
    validate_completed_markets(db_path)
    export_path = tmp_path / "reports" / "signal_weight_recommendation.json"

    signal_weight_report_cli(as_json=False, db_path=str(db_path), export_path=str(export_path), window_days=90)
    report_output = capsys.readouterr().out
    assert "Signal Weight Optimization" in report_output
    assert export_path.exists()

    signal_weight_optimize_cli(as_json=False, db_path=str(db_path), export_path=str(export_path), dry_run=True)
    optimize_output = capsys.readouterr().out
    assert "Dry-run only" in optimize_output


def test_format_weight_optimization_cli_includes_table():
    report = {
        "sample_size": 6,
        "confidence": 0.42,
        "recommendations": [
            {
                "component": "wallet_performance",
                "current_weight": 0.10,
                "suggested_weight": 0.11,
                "delta": 0.01,
                "confidence": 0.42,
            }
        ],
        "rationale": "test rationale",
    }
    text = format_weight_optimization_cli(report)
    assert "wallet_performance" in text
    assert "test rationale" in text
