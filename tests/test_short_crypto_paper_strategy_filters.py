from __future__ import annotations

import json
import sqlite3
import time

import src.analysis.short_crypto_paper as paper
from src.analysis.short_crypto_paper import (
    PaperConfig,
    ShortCryptoPaperStore,
    performance_report,
    run_paper,
    validate_strategy_filters,
)
from src.analysis.short_crypto_markets import ShortCryptoMarket


def _market(**overrides):
    now = time.time()
    data = {
        "asset": "BTC",
        "venue": "polymarket",
        "ticker": "btc-updown-5m-test",
        "start_ts": now - 60,
        "end_ts": now + 300,
        "direction": "down",
        "yes_bid": 0.47,
        "yes_ask": 0.49,
        "no_bid": 0.51,
        "no_ask": 0.53,
        "liquidity": 10.0,
        "window_minutes": 5,
        "timestamp": now,
        "raw": {"slug": "btc-updown-5m-test", "token_id": "123"},
    }
    data.update(overrides)
    return ShortCryptoMarket(**data)


def _run_with_markets(tmp_path, config: PaperConfig, markets: list[ShortCryptoMarket], monkeypatch):
    monkeypatch.setattr(paper, "discover_polymarket_series_markets", lambda *_a, **_k: markets)
    monkeypatch.setattr(paper, "discover_markets", lambda cfg, errors, counters: markets)
    monkeypatch.setattr(paper, "fetch_spot_prices", lambda assets: {"BTC": 61000.0})
    return run_paper(config)


def test_direction_filter_rejects_non_matching_direction(tmp_path, monkeypatch):
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=5,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
        directions=("down",),
    )
    markets = [
        _market(ticker="up-1", direction="up"),
        _market(ticker="down-1", direction="down"),
    ]
    result = _run_with_markets(tmp_path, cfg, markets, monkeypatch)
    assert result["paper_trades_created"] == 1
    assert result["rejected_by_direction"] == 1
    with ShortCryptoPaperStore(str(tmp_path / "paper.db")).connect() as conn:
        row = conn.execute("SELECT direction FROM paper_trades").fetchone()
    assert row[0] == "down"


def test_max_model_probability_filter(tmp_path, monkeypatch):
    original_build = paper.build_intent

    def high_model_intent(market, spot_price, now_ts=None):
        intent = original_build(market, spot_price, now_ts=now_ts)
        intent["model_probability"] = 0.62
        intent["edge"] = 0.10
        return intent

    monkeypatch.setattr(paper, "build_intent", high_model_intent)
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=5,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
        max_model_probability=0.55,
    )
    result = _run_with_markets(tmp_path, cfg, [_market()], monkeypatch)
    assert result["paper_trades_created"] == 0
    assert result["rejected_by_model_probability"] == 1


def test_max_entry_price_filter(tmp_path, monkeypatch):
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=5,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
        max_entry_price=0.50,
    )
    expensive = _market(yes_ask=0.55, yes_bid=0.54)
    cheap = _market(ticker="cheap", yes_ask=0.48, yes_bid=0.47)
    result = _run_with_markets(tmp_path, cfg, [expensive, cheap], monkeypatch)
    assert result["paper_trades_created"] == 1
    assert result["rejected_by_entry_price"] == 1


def test_strategy_label_persisted_in_raw_json(tmp_path, monkeypatch):
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=1,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
        strategy_label="down_ask50_prob55",
    )
    result = _run_with_markets(tmp_path, cfg, [_market()], monkeypatch)
    assert result["paper_trades_created"] == 1
    assert result["strategy_label"] == "down_ask50_prob55"
    with ShortCryptoPaperStore(str(tmp_path / "paper.db")).connect() as conn:
        row = conn.execute("SELECT strategy_label, raw_json FROM paper_trades").fetchone()
    payload = json.loads(row["raw_json"])
    assert row["strategy_label"] == "down_ask50_prob55"
    assert payload["strategy_label"] == "down_ask50_prob55"


def test_by_strategy_label_report_grouping(tmp_path, monkeypatch):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], db_path=str(tmp_path / "paper.db"))
    run_id = store.start_run(cfg)
    intent = paper.build_intent(_market(), 61000.0)
    intent.update(paper.simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    intent["strategy_label"] = "down_ask50_prob55"
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
    trade = {"id": tid, **intent, "paper_cost": intent["paper_cost"]}
    store.mark_settled(
        trade,
        {"result": "won", "payout": 1.0, "pnl": 0.51, "roi": 1.0, "settlement_source": "test", "reason": "test"},
    )
    report = performance_report(str(tmp_path / "paper.db"))
    assert "down_ask50_prob55" in report["by_strategy_label"]
    group = report["by_strategy_label"]["down_ask50_prob55"]
    assert group["closed_trades"] == 1
    assert group["win_rate"] == 1.0
    assert group["roi"] == 0.51 / intent["paper_cost"]
    assert group["pnl"] == 0.51
    assert group["expectancy"] == 0.51


def test_volatility_filter_rejection_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "_entry_volatility", lambda intent: 0.01)
    monkeypatch.setattr(paper, "_historical_volatility_median", lambda db_path: 0.02)
    assert (
        validate_strategy_filters(
            {"direction": "down", "model_probability": 0.52, "entry_price": 0.49, "entry_time": paper._iso(time.time()), "spot_price": 61000},
            PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], require_volatility_above_median=True),
            volatility_median=0.02,
        )
        == "volatility_filter"
    )
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=5,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
        require_volatility_above_median=True,
    )
    result = _run_with_markets(tmp_path, cfg, [_market()], monkeypatch)
    assert result["paper_trades_created"] == 0
    assert result["rejected_by_volatility"] == 1


def test_default_runner_behavior_unchanged_without_filters(tmp_path, monkeypatch):
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=2,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
    )
    markets = [_market(direction="up"), _market(ticker="down", direction="down")]
    result = _run_with_markets(tmp_path, cfg, markets, monkeypatch)
    assert result["paper_trades_created"] == 2
    assert result["rejected_by_direction"] == 0
    assert result["rejected_by_model_probability"] == 0
    assert result["rejected_by_entry_price"] == 0
    assert result["strategy_label"] is None


def test_migration_adds_strategy_label_and_backfills_raw_json(tmp_path):
    db_path = tmp_path / "paper.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE paper_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                venues TEXT NOT NULL,
                assets TEXT NOT NULL,
                windows TEXT NOT NULL,
                max_trades INTEGER NOT NULL,
                created_trades INTEGER NOT NULL DEFAULT 0,
                rejected_signals INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE paper_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                created_at TEXT NOT NULL,
                venue TEXT NOT NULL,
                asset TEXT NOT NULL,
                market TEXT,
                ticker TEXT,
                slug TEXT,
                token_id TEXT,
                direction TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                window_minutes INTEGER NOT NULL,
                entry_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                spot_price REAL,
                model_probability REAL,
                implied_probability REAL,
                edge REAL,
                size REAL,
                status TEXT NOT NULL,
                rejection_reason TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                run_id INTEGER,
                created_at TEXT NOT NULL,
                venue TEXT NOT NULL,
                asset TEXT NOT NULL,
                market TEXT,
                ticker TEXT,
                slug TEXT,
                token_id TEXT,
                direction TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                window_minutes INTEGER NOT NULL,
                entry_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                model_probability REAL NOT NULL,
                implied_probability REAL NOT NULL,
                edge REAL NOT NULL,
                size REAL NOT NULL,
                paper_cost REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                expected_value REAL NOT NULL,
                status TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE paper_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL UNIQUE,
                settled_at TEXT NOT NULL,
                result TEXT NOT NULL,
                payout REAL,
                pnl REAL,
                roi REAL,
                settlement_source TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            """
        )
        intent = paper.build_intent(_market(), 61000.0)
        intent.update(paper.simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        intent["strategy_label"] = "raw_label"
        conn.execute(
            """
            INSERT INTO paper_trades
            (signal_id, run_id, created_at, venue, asset, market, ticker, slug, token_id, direction, side, action,
             window_minutes, entry_time, end_time, entry_price, model_probability, implied_probability, edge,
             size, paper_cost, fee, expected_value, status, raw_json)
            VALUES (1, 1, '2026-06-09T00:00:00+00:00', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                intent["venue"],
                intent["asset"],
                intent.get("market"),
                intent.get("ticker"),
                intent.get("slug"),
                intent.get("token_id"),
                intent["direction"],
                intent["side"],
                intent["action"],
                int(intent["window_minutes"]),
                intent["entry_time"],
                intent["end_time"],
                float(intent["entry_price"]),
                float(intent["model_probability"]),
                float(intent["implied_probability"]),
                float(intent["edge"]),
                float(intent["size"]),
                float(intent["paper_cost"]),
                float(intent.get("fee", 0.0)),
                float(intent["expected_value"]),
                json.dumps(intent),
            ),
        )

    ShortCryptoPaperStore(str(db_path))

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)")}
        row = conn.execute("SELECT strategy_label FROM paper_trades").fetchone()
    assert "strategy_label" in columns
    assert row[0] == "raw_label"


def test_legacy_unlabeled_trades_group_as_unlabeled(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], db_path=str(tmp_path / "paper.db"))
    run_id = store.start_run(cfg)
    intent = paper.build_intent(_market(), 61000.0)
    intent.update(paper.simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    store.save_trade(intent, signal_id=sid, run_id=run_id)

    report = performance_report(str(tmp_path / "paper.db"))

    assert report["by_strategy_label"]["unlabeled"]["total_trades"] == 1
    assert report["by_strategy_label"]["unlabeled"]["open_trades"] == 1


def test_open_labeled_trades_appear_in_strategy_report_before_settlement(tmp_path, monkeypatch):
    cfg = PaperConfig(
        venues=["polymarket"],
        assets=["BTC"],
        windows=[5],
        max_trades=1,
        min_edge=-1.0,
        db_path=str(tmp_path / "paper.db"),
        strategy_label="down_only",
    )
    result = _run_with_markets(tmp_path, cfg, [_market()], monkeypatch)

    report = performance_report(str(tmp_path / "paper.db"))

    assert result["paper_trades_created"] == 1
    assert report["by_strategy_label"]["down_only"]["total_trades"] == 1
    assert report["by_strategy_label"]["down_only"]["open_trades"] == 1
    assert report["by_strategy_label"]["down_only"]["closed_trades"] == 0
