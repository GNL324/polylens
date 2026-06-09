from __future__ import annotations

import json
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
        raw = conn.execute("SELECT raw_json FROM paper_trades").fetchone()[0]
    payload = json.loads(raw)
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
    assert group["pnl"] == 0.51
    assert group["expectancy"] is not None


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
