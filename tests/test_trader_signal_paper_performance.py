from __future__ import annotations

import json
import sys

from src.analysis.trader_signal_engine import init_trader_signal_db
from src.analysis.trader_signal_paper_bridge import init_trader_signal_paper_bridge_db
from src.analysis.trader_signal_paper_performance import (
    paper_strategy_performance_report,
    settle_paper_strategy_positions,
    sync_paper_strategy_positions_from_intents,
)
from src.analysis.trader_signal_validation import init_trader_signal_validation_db
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


def _seed_signal_intent_and_validation(db_path, *, signal_id="sig-1", signal_type="early_entry", correct=1, price=0.4):
    init_trader_signal_db(db_path)
    init_trader_signal_paper_bridge_db(db_path)
    init_trader_signal_validation_db(db_path)
    recommendation_id = f"{signal_id}:paper_entry"
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trader_signals (
                signal_key, wallet, market_id, market_title, asset, side, signal_type,
                timestamp, action, price, shares, amount, score, tx_hash, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                WALLET,
                "market-1",
                "Bitcoin Up or Down",
                "BTC",
                "up",
                signal_type,
                100.0,
                "buy",
                price,
                10.0,
                100.0,
                80.0,
                f"0x{signal_id}",
                "{}",
                "2026-06-01T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO trader_signal_paper_intents (
                intent_key, recommendation_id, market_id, side, recommendation_type,
                signal_type, trader_address, score, validation_count, historical_accuracy,
                gate_status, gate_reason, notional_usd, status, reason, created_at,
                read_only, paper_only
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
            """,
            (
                f"{recommendation_id}:simulated",
                recommendation_id,
                "market-1",
                "up",
                "paper_entry",
                signal_type,
                WALLET,
                80.0,
                20,
                0.6,
                "proven",
                "signal family meets validation and accuracy thresholds",
                10.0,
                "simulated",
                "simulated paper-copy intent from proven paper_entry recommendation",
                "2026-06-01T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO trader_signal_validation (
                validation_key, recommendation_id, signal_id, market_id, signal_type,
                recommendation_type, generated_at, resolved_at, outcome, predicted_side,
                correct, roi_proxy, confidence, trader_address, validation_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"validation-{signal_id}",
                recommendation_id,
                signal_id,
                "market-1",
                signal_type,
                "paper_entry",
                "2026-06-01T00:00:00Z",
                "2026-06-02T00:00:00Z",
                "win" if correct else "loss",
                "up",
                correct,
                0.5 if correct else -0.5,
                80.0,
                WALLET,
                "2026-06-02T00:00:00Z",
            ),
        )


def test_paper_strategy_settlement_and_roi(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signal_intent_and_validation(db_path, price=0.4)

    sync = sync_paper_strategy_positions_from_intents(db_path)
    settlement = settle_paper_strategy_positions(db_path)

    assert sync["read_only"] is True
    assert sync["paper_only"] is True
    assert sync["positions_inserted"] == 1
    assert settlement["positions_settled"] == 1
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT pnl, roi, status FROM paper_strategy_positions").fetchone()
    assert row["status"] == "closed"
    assert row["pnl"] == 15.0
    assert row["roi"] == 1.5


def test_paper_strategy_attribution_aggregation(tmp_path):
    db_path = tmp_path / "signals.db"
    _seed_signal_intent_and_validation(db_path, signal_id="sig-win", correct=1, price=0.5)
    _seed_signal_intent_and_validation(db_path, signal_id="sig-loss", correct=0, price=0.5)

    report = paper_strategy_performance_report(db_path)
    early_entry = report["by_strategy"][0]

    assert report["read_only"] is True
    assert report["paper_only"] is True
    assert report["summary"]["trades"] == 2
    assert report["summary"]["wins"] == 1
    assert report["summary"]["losses"] == 1
    assert report["summary"]["win_rate"] == 0.5
    assert early_entry["signal_family"] == "early_entry"
    assert early_entry["strategy_label"] == "wallet_signal:early_entry"
    assert early_entry["recommendation_type"] == "paper_entry"
    assert early_entry["trades"] == 2
    assert early_entry["expectancy"] == 0.0
    assert report["daily_stats"]


def test_paper_performance_report_cli_signal_db(tmp_path, capsys):
    db_path = tmp_path / "trader_signals.db"
    _seed_signal_intent_and_validation(db_path)

    from src.cli import main

    sys.argv = ["polylens", "paper-performance-report", "--db-path", str(db_path), "--json"]
    main()

    output = json.loads(capsys.readouterr().out)
    assert output["read_only"] is True
    assert output["paper_only"] is True
    assert output["summary"]["trades"] == 1
    assert output["by_strategy"][0]["signal_family"] == "early_entry"
