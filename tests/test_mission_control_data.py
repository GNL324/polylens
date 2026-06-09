from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.short_crypto_paper import ShortCryptoPaperStore
from src.web.mission_control_data import (
    compute_streak,
    format_pct,
    format_pnl,
    format_price,
    mission_control_snapshot,
    pnl_series_24h,
    render_line_svg,
    trades_per_day,
)


def _seed_paper_db(db_path) -> None:
    store = ShortCryptoPaperStore(str(db_path))
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_runs (started_at, completed_at, venues, assets, windows, max_trades, created_trades, rejected_signals, raw_json)
            VALUES ('2026-06-09T10:00:00Z', '2026-06-09T10:00:05Z', 'polymarket', 'BTC', '5', 3, 1, 2, ?)
            """,
            (
                json.dumps(
                    {
                        "run_id": 1,
                        "markets_seen": 5,
                        "paper_trades_created": 1,
                        "rejected_signals": 2,
                        "strategy_label": "test_strategy",
                        "strategy_filters": {"strategy_label": "test_strategy"},
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_signals (run_id, created_at, venue, asset, direction, side, action, window_minutes,
                entry_time, end_time, model_probability, implied_probability, edge, size, status, rejection_reason, raw_json)
            VALUES (1, '2026-06-09T10:00:01Z', 'polymarket', 'BTC', 'up', 'yes', 'BUY', 5,
                '2026-06-09T10:00:00Z', '2026-06-09T10:05:00Z', 0.6, 0.5, 0.1, 1.0, 'accepted', NULL, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO paper_trades (signal_id, run_id, created_at, venue, asset, direction, side, action, window_minutes,
                entry_time, end_time, entry_price, model_probability, implied_probability, edge, size, paper_cost, fee,
                expected_value, status, strategy_label, raw_json)
            VALUES (1, 1, '2026-06-09T10:00:01Z', 'polymarket', 'BTC', 'up', 'yes', 'BUY', 5,
                '2026-06-09T10:00:00Z', '2026-06-09T10:05:00Z', 0.5, 0.6, 0.5, 0.1, 1.0, 0.5, 0.0,
                0.1, 'open', 'test_strategy', ?)
            """,
            (json.dumps({"spot_price": 65000.0, "strategy_label": "test_strategy"}),),
        )
        conn.execute(
            """
            INSERT INTO paper_trades (signal_id, run_id, created_at, venue, asset, direction, side, action, window_minutes,
                entry_time, end_time, entry_price, model_probability, implied_probability, edge, size, paper_cost, fee,
                expected_value, status, strategy_label, raw_json)
            VALUES (1, 1, '2026-06-08T12:00:00Z', 'polymarket', 'BTC', 'down', 'no', 'BUY', 5,
                '2026-06-08T12:00:00Z', '2026-06-08T12:05:00Z', 0.5, 0.4, 0.5, 0.05, 1.0, 0.5, 0.0,
                -0.1, 'won', 'test_strategy', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO paper_trades (signal_id, run_id, created_at, venue, asset, direction, side, action, window_minutes,
                entry_time, end_time, entry_price, model_probability, implied_probability, edge, size, paper_cost, fee,
                expected_value, status, strategy_label, raw_json)
            VALUES (1, 1, '2026-06-09T08:00:00Z', 'polymarket', 'BTC', 'down', 'no', 'BUY', 5,
                '2026-06-09T08:00:00Z', '2026-06-09T08:05:00Z', 0.5, 0.4, 0.5, 0.05, 1.0, 0.5, 0.0,
                -0.1, 'lost', 'test_strategy', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO paper_settlements (trade_id, settled_at, result, payout, pnl, roi, settlement_source, raw_json)
            VALUES (2, '2026-06-08T12:10:00Z', 'won', 1.0, 0.5, 1.0, 'test', '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO paper_settlements (trade_id, settled_at, result, payout, pnl, roi, settlement_source, raw_json)
            VALUES (3, '2026-06-09T09:00:00Z', 'lost', 0.0, -0.4, -0.8, 'test', '{}')
            """
        )


def test_format_helpers() -> None:
    assert format_pnl(1.25) == "+$1.25"
    assert format_pnl(-2.0) == "$-2.00"
    assert format_pnl(None) == "—"
    assert format_pct(0.512) == "51.2%"
    assert format_price(62000.5) == "$62,000.50"


def test_compute_streak(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_paper_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO paper_trades (signal_id, run_id, created_at, venue, asset, direction, side, action, window_minutes,
                entry_time, end_time, entry_price, model_probability, implied_probability, edge, size, paper_cost, fee,
                expected_value, status, strategy_label, raw_json)
            VALUES (1, 1, '2026-06-08T11:00:00Z', 'polymarket', 'BTC', 'down', 'no', 'BUY', 5,
                '2026-06-08T11:00:00Z', '2026-06-08T11:05:00Z', 0.5, 0.4, 0.5, 0.05, 1.0, 0.5, 0.0,
                -0.1, 'lost', 'test_strategy', '{}')
            """
        )
        trade_id = conn.execute("SELECT id FROM paper_trades ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute(
            """
            INSERT INTO paper_settlements (trade_id, settled_at, result, payout, pnl, roi, settlement_source, raw_json)
            VALUES (?, '2026-06-08T11:10:00Z', 'lost', 0.0, -0.5, -1.0, 'test', '{}')
            """,
            (trade_id,),
        )
    streak = compute_streak(db_path)
    assert streak["type"] == "lost"
    assert streak["count"] == 1


def test_pnl_series_24h(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_paper_db(db_path)
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    series = pnl_series_24h(db_path, now=now)
    assert series["current_pnl"] == 0.1
    assert len(series["points"]) >= 1
    assert "svg" in series
    assert "<svg" in series["svg"]


def test_render_line_svg_empty() -> None:
    svg = render_line_svg([], value_key="pnl")
    assert "<svg" in svg


def test_trades_per_day(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    _seed_paper_db(db_path)
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    rate = trades_per_day(db_path, now=now, days=2)
    assert rate == 1.5


def test_mission_control_snapshot(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "paper.db"
    polylens_db = tmp_path / "polylens.db"
    _seed_paper_db(db_path)
    monkeypatch.setattr(
        "src.web.mission_control_data._api_connectivity",
        lambda: {"kalshi": "ok", "polymarket": "ok"},
    )
    monkeypatch.setattr(
        "src.web.mission_control_data._systemd_state",
        lambda unit: "active",
    )
    monkeypatch.setattr(
        "src.web.mission_control_data.fetch_spot_prices",
        lambda assets: {"BTC": 65000.0},
    )
    monkeypatch.setattr(
        "src.web.mission_control_data._coinbase_stats",
        lambda symbol: {"last": "65000", "open": "64000"},
    )
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    snapshot = mission_control_snapshot(paper_db_path=db_path, polylens_db_path=polylens_db, now=now)
    assert snapshot["header"]["strategy_label"] == "test_strategy"
    assert snapshot["kpis"]["total_trades"] == 3
    assert snapshot["active_bet"] is not None
    assert snapshot["active_bet"]["side"] == "UP"
    assert len(snapshot["pipeline"]) == 6
    assert snapshot["system_health"]["short_crypto_timer"] == "active"
