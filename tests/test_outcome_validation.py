from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.analysis.outcome_metrics import brier_score, calibration_curve, validation_metrics
from src.analysis.outcome_validator import (
    outcome_validation_report,
    rebuild_validation_metrics,
    validate_completed_markets,
)
from src.analysis.paper_trading_engine import init_paper_trading_db
from src.integrations.telegram_console import TelegramConsole, TelegramConsoleConfig
from src.integrations.telegram_notifications import outcome_validation_report_text
from src.web.mission_control import MISSION_CONTROL_NAV_ITEMS
from src.web.mission_control_data import mission_control_snapshot


def _seed_validation_db(db_path) -> None:
    init_paper_trading_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_runs (id, run_timestamp, opportunities_seen, orders_created, positions_opened, positions_settled, equity, raw_json)
            VALUES (1, '2026-06-25T09:00:00Z', 3, 3, 3, 0, 100, '{}')
            """
        )
        for index, (wallet, strategy, confidence, settlement_price, category, expiry) in enumerate(
            [
                ("0xwallet1", "copy_alpha", 0.80, 1.0, "politics", 3600),
                ("0xwallet1", "copy_alpha", 0.70, 0.0, "politics", 7200),
                ("0xwallet2", "signal_beta", 0.60, 0.5, "sports", 604800),
            ],
            start=1,
        ):
            conn.execute(
                """
                INSERT INTO paper_orders
                (id, run_id, opportunity_id, strategy, side, market_id, title, asset, simulated_price, stake, status, raw_json, created_timestamp)
                VALUES (?, 1, ?, ?, 'yes', ?, ?, 'POLY', 0.50, 10, 'filled', ?, '2026-06-25T09:00:00Z')
                """,
                (
                    index,
                    f"opp-{index}",
                    strategy,
                    f"market-{index}",
                    f"Market {index}",
                    (
                        '{"confidence": %.2f, "ranking_score": %.2f, "execution": {"quality": 0.91}, '
                        '"portfolio_state": {"cash_balance": 90, "total_equity": 100}}'
                    )
                    % (confidence, 0.9 - index / 10),
                ),
            )
            conn.execute(
                """
                INSERT INTO paper_positions
                (paper_position_id, order_id, opportunity_id, strategy, market_id, title, asset, side,
                 entry_timestamp, entry_price, shares, notional, status, current_price, exit_timestamp,
                 exit_price, realized_pnl, unrealized_pnl, roi, signal_family, source_wallet, confidence,
                 validation_score, market_category, market_liquidity, time_to_expiry_seconds)
                VALUES (?, ?, ?, ?, ?, ?, 'POLY', 'yes',
                    '2026-06-25T09:00:00Z', 0.50, 20, 10, 'closed', ?, '2026-06-25T10:00:00Z',
                    ?, ?, 0, ?, 'family_a', ?, ?, 0.4, ?, 1000, ?)
                """,
                (
                    index,
                    index,
                    f"opp-{index}",
                    strategy,
                    f"market-{index}",
                    f"Market {index}",
                    settlement_price,
                    settlement_price,
                    round((settlement_price - 0.5) * 20, 6),
                    round(((settlement_price - 0.5) * 20) / 10, 6),
                    wallet,
                    confidence,
                    category,
                    expiry,
                ),
            )
            conn.execute(
                """
                INSERT INTO paper_settlements (run_id, paper_position_id, exit_timestamp, exit_price, pnl, roi, reason)
                VALUES (1, ?, '2026-06-25T10:00:00Z', ?, ?, 0, 'test')
                """,
                (index, settlement_price, round((settlement_price - 0.5) * 20, 6)),
            )


def test_outcome_validation_attribution_and_brier(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_validation_db(db_path)

    result = validate_completed_markets(db_path)

    assert result["paper_only"] is True
    assert result["validated"] == 3
    assert result["correct"] == 1
    assert result["incorrect"] == 1
    assert result["partial"] == 1
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM outcome_validations ORDER BY paper_position_id").fetchall()
    assert [row["validation_status"] for row in rows] == ["correct", "incorrect", "partial"]
    assert rows[0]["strategy"] == "copy_alpha"
    assert rows[0]["copied_wallet"] == "0xwallet1"
    assert rows[0]["signal_family"] == "family_a"
    assert rows[0]["market_category"] == "politics"
    assert rows[0]["brier_score"] == brier_score(0.80, 1.0)


def test_calibration_and_rolling_metrics() -> None:
    rows = [
        {"confidence_at_entry": 0.8, "correctness_score": 1, "validation_status": "correct", "brier_score": brier_score(0.8, 1), "strategy": "a"},
        {"confidence_at_entry": 0.7, "correctness_score": 0, "validation_status": "incorrect", "brier_score": brier_score(0.7, 0), "strategy": "a"},
        {"confidence_at_entry": 0.6, "correctness_score": 0.5, "validation_status": "partial", "brier_score": brier_score(0.6, 0.5), "strategy": "b"},
    ]

    metrics = validation_metrics(rows)
    curve = calibration_curve(rows)

    assert metrics["resolved_count"] == 3
    assert metrics["accuracy"] == 0.5
    assert metrics["brier_score"] == round((0.04 + 0.49 + 0.01) / 3, 6)
    assert curve
    assert {item["bucket"] for item in curve} >= {0.6, 0.7, 0.8}


def test_outcome_backfill_is_resumable(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_validation_db(db_path)

    first = rebuild_validation_metrics(db_path, batch_size=2, reset=True)
    second = rebuild_validation_metrics(db_path, batch_size=2)

    assert first["status"] == "running"
    assert first["validated"] == 2
    assert second["status"] == "complete"
    assert second["validated"] == 1
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM outcome_validations").fetchone()[0]
        state = conn.execute("SELECT status, processed_count FROM outcome_backfill_state WHERE key='canonical_positions'").fetchone()
    assert count == 3
    assert state == ("complete", 3)


def test_outcome_report_attribution_and_adjustments(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_validation_db(db_path)
    validate_completed_markets(db_path)

    report = outcome_validation_report(db_path, now=datetime(2026, 6, 26, tzinfo=timezone.utc))

    assert report["validation_accuracy"] == 0.5
    assert report["todays_resolved_markets"] == 0
    assert report["by_wallet"]["0xwallet1"]["resolved_count"] == 2
    assert report["by_strategy"]["copy_alpha"]["accuracy"] == 0.5
    assert report["confidence_adjustments"]["copied_wallet"][0]["auto_apply"] is False
    assert report["best_wallets"]
    assert report["worst_strategies"]


def test_mission_control_outcome_snapshot_and_tab(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "paper.db"
    short_db = tmp_path / "short.db"
    polylens_db = tmp_path / "polylens.db"
    _seed_validation_db(db_path)
    validate_completed_markets(db_path)
    short_db.touch()
    monkeypatch.setattr("src.web.mission_control_data._api_connectivity", lambda: {"kalshi": "ok", "polymarket": "ok"})
    monkeypatch.setattr("src.web.mission_control_data._systemd_state", lambda unit: "active")

    snapshot = mission_control_snapshot(
        paper_db_path=short_db,
        polylens_db_path=polylens_db,
        portfolio_db_path=db_path,
        now=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    assert "Outcome Validation" in MISSION_CONTROL_NAV_ITEMS
    assert snapshot["outcome_validation"]["validation_accuracy"] == 0.5
    assert snapshot["outcome_validation"]["best_strategies"]


def test_telegram_outcome_validation_rendering_and_navigation(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_validation_db(db_path)
    validate_completed_markets(db_path)
    report = outcome_validation_report(db_path, now=datetime(2026, 6, 26, tzinfo=timezone.utc))
    text = outcome_validation_report_text(report)
    assert "📈 Outcome Validation" in text
    assert "Brier score" in text

    paper_payload = {
        "paper_only": True,
        "outcome_validation": report,
        "recent_trades": [],
        "trade_count": 0,
        "open_positions_count": 0,
        "closed_positions_count": 0,
        "win_rate": 0,
    }
    console = TelegramConsole(
        TelegramConsoleConfig(
            bot_token="token",
            admin_user_ids=frozenset({123}),
            audit_db_path=str(tmp_path / "telegram.db"),
            paper_db_path=str(db_path),
            short_crypto_paper_db_path=str(tmp_path / "short.db"),
        ),
        paper_intelligence_provider=lambda: paper_payload,
    )
    command = console.handle_text(123, "/outcome_validation")
    nav = console.handle_callback(123, "nav_outcome")
    assert "Outcome Validation" in command.text
    assert "Outcome Validation" in nav.text
