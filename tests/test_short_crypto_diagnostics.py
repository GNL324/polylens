from __future__ import annotations

import json
import time

from src.analysis.short_crypto_diagnostics import (
    ShortCryptoDiagnosticsStore,
    calibration_report,
    diagnostics_report,
    edge_discovery_report,
    extract_trade_features,
    recommendation_report,
    verbose_report_extensions,
)
from src.analysis.short_crypto_paper import (
    PaperConfig,
    ShortCryptoPaperStore,
    build_intent,
    simulate_fill,
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
        "direction": "up",
        "yes_bid": 0.48,
        "yes_ask": 0.51,
        "no_bid": 0.49,
        "no_ask": 0.52,
        "liquidity": 10.0,
        "window_minutes": 5,
        "timestamp": now,
        "raw": {"slug": "btc-updown-5m-test", "token_id": "123"},
    }
    data.update(overrides)
    return ShortCryptoMarket(**data)


def _config(db_path):
    return PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], db_path=str(db_path))


def _seed_closed_trade(store: ShortCryptoPaperStore, *, ticker: str, won: bool, model_probability: float, ask: float = 0.51):
    cfg = _config(store.db_path)
    run_id = store.start_run(cfg)
    intent = build_intent(_market(ticker=ticker, yes_ask=ask, yes_bid=ask - 0.01), 61400.0)
    intent["model_probability"] = model_probability
    intent["implied_probability"] = ask
    intent["edge"] = model_probability - ask
    intent.update(simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
    trade = {"id": tid, **intent, "paper_cost": intent["paper_cost"], "fee": 0.0}
    result = "won" if won else "lost"
    payout = 1.0 if won else 0.0
    pnl = payout - intent["paper_cost"]
    store.mark_settled(
        trade,
        {
            "result": result,
            "payout": payout,
            "pnl": pnl,
            "roi": pnl / intent["paper_cost"],
            "settlement_source": "test",
            "reason": "test",
        },
    )
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (tid,)).fetchone()
        settlement = conn.execute("SELECT * FROM paper_settlements WHERE trade_id=?", (tid,)).fetchone()
    return dict(row), dict(settlement)


def test_feature_extraction_captures_core_and_market_fields(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    trade, settlement = _seed_closed_trade(store, ticker="feat-1", won=True, model_probability=0.58)
    feature = extract_trade_features(trade, settlement)
    assert feature["trade_id"] == trade["id"]
    assert feature["venue"] == "polymarket"
    assert feature["asset"] == "BTC"
    assert feature["slug"] == "btc-updown-5m-test"
    assert feature["direction"] == "up"
    assert feature["entry_price"] == trade["entry_price"]
    assert feature["bid"] is not None
    assert feature["ask"] is not None
    assert feature["spread"] is not None
    assert feature["model_probability"] == 0.58
    assert feature["outcome"] == "won"
    assert feature["pnl"] is not None
    assert feature["hour_of_day_utc"] is not None
    assert feature["minute_bucket"] in range(0, 60)
    assert feature["window_minutes"] == 5
    assert feature["seconds_to_expiry_at_entry"] > 0
    assert feature["btc_price_at_entry"] == 61400.0


def test_features_table_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    _seed_closed_trade(store, ticker="mig-1", won=True, model_probability=0.55)
    diag = ShortCryptoDiagnosticsStore(db_path)
    diag.ensure_schema()
    diag.ensure_schema()
    sync = diag.sync_features()
    assert sync["features_inserted"] == 1
    assert diag.feature_count() == 1
    second = diag.sync_features()
    assert second["features_inserted"] == 0
    assert second["features_total"] == 1


def test_calibration_math_and_probability_buckets(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    for idx in range(10):
        _seed_closed_trade(store, ticker=f"win-{idx}", won=True, model_probability=0.53)
    for idx in range(10):
        _seed_closed_trade(store, ticker=f"loss-{idx}", won=False, model_probability=0.53)
    monkeypatch.setattr("src.analysis.short_crypto_diagnostics._fetch_btc_candles_around", lambda *_a, **_k: [])
    report = calibration_report(db_path, refresh_features=True)
    bucket = next(item for item in report["calibration"] if item["bucket"] == "50-55%")
    assert bucket["count"] == 20
    assert bucket["predicted"] == 0.53
    assert bucket["actual"] == 0.5


def test_diagnostics_bucket_calculations(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    _seed_closed_trade(store, ticker="up-win", won=True, model_probability=0.60)
    _seed_closed_trade(store, ticker="up-loss", won=False, model_probability=0.60)
    monkeypatch.setattr("src.analysis.short_crypto_diagnostics._fetch_btc_candles_around", lambda *_a, **_k: [])
    report = diagnostics_report(db_path, refresh_features=True)
    assert report["total_closed_trades"] == 2
    assert report["overall_win_rate"] == 0.5
    assert "up" in report["direction"]
    assert report["direction"]["up"]["trades"] == 2
    assert "5" in report["window_size"]


def test_edge_discovery_respects_minimum_sample_size(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    for idx in range(25):
        _seed_closed_trade(store, ticker=f"seg-{idx}", won=idx % 2 == 0, model_probability=0.62, ask=0.47)
    monkeypatch.setattr("src.analysis.short_crypto_diagnostics._fetch_btc_candles_around", lambda *_a, **_k: [])
    report = edge_discovery_report(db_path, refresh_features=True, min_segment_trades=20)
    assert report["min_segment_trades"] == 20
    assert any("model_probability>0.60" in item["segment"] for item in report["best_segments"] + report["worst_segments"] or [])
    small = edge_discovery_report(db_path, refresh_features=False, min_segment_trades=100)
    assert small["best_segments"] == []


def test_recommendation_and_verbose_report_generation(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    for idx in range(25):
        _seed_closed_trade(store, ticker=f"rec-{idx}", won=False, model_probability=0.58)
    monkeypatch.setattr("src.analysis.short_crypto_diagnostics._fetch_btc_candles_around", lambda *_a, **_k: [])
    recs = recommendation_report(db_path, refresh_features=True, min_segment_trades=20)
    assert recs["recommendations"]
    assert any(item["status"] == "disable" for item in recs["recommendations"])
    verbose = verbose_report_extensions(db_path, refresh_features=False, min_segment_trades=20)
    assert verbose["calibration_summary"]
    assert verbose["best_segment_summary"] is not None
    assert verbose["worst_segment_summary"] is not None
    assert verbose["recommendation"]["status"] in {"disable", "enable", "prefer"}


def test_sync_features_persists_json_payload(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    _seed_closed_trade(store, ticker="persist-1", won=True, model_probability=0.57)
    monkeypatch.setattr("src.analysis.short_crypto_diagnostics._fetch_btc_candles_around", lambda *_a, **_k: [])
    diag = ShortCryptoDiagnosticsStore(db_path)
    diag.sync_features()
    with diag.connect() as conn:
        row = conn.execute("SELECT feature_json FROM paper_trade_features").fetchone()
    payload = json.loads(row[0])
    assert payload["trade_id"] == 1
    assert payload["model_probability"] == 0.57



def test_diagnostics_regime_and_segment_metrics(tmp_path, monkeypatch):
    db_path = str(tmp_path / "paper.db")
    store = ShortCryptoPaperStore(db_path)
    _seed_closed_trade(store, ticker="regime-win", won=True, model_probability=0.60, ask=0.51)
    _seed_closed_trade(store, ticker="regime-loss", won=False, model_probability=0.60, ask=0.56)

    candles = [[idx, 0, 0, 0, 60000.0 + idx * 10.0] for idx in range(62)]
    monkeypatch.setattr("src.analysis.short_crypto_diagnostics._fetch_btc_candles_around", lambda *_a, **_k: candles)

    report = diagnostics_report(db_path, refresh_features=True)

    assert report["overall_expectancy"] is not None
    assert "0-30" in report["lead_time_buckets"]
    assert report["lead_time_buckets"]["0-30"]["expectancy"] is not None
    assert report["lead_time_buckets"]["0-30"]["pnl"] is not None
    assert "0.50-0.55" in report["price_band_analysis"]
    assert "0.55-0.60" in report["price_band_analysis"]
    assert report["price_band_analysis"]["0.50-0.55"]["expectancy"] is not None
    assert "positive" in report["btc_regime_analysis"]["btc_return_5m"]
    assert "positive" in report["btc_regime_analysis"]["btc_return_15m"]
    assert "positive" in report["btc_regime_analysis"]["btc_return_1h"]
    assert report["btc_regime_analysis"]["volatility_percentile"]

    discovery = edge_discovery_report(db_path, refresh_features=False, min_segment_trades=1)
    segment = next(item for item in discovery["best_segments"] + discovery["worst_segments"] if "model_probability" in item["segment"])
    assert "expectancy" in segment
    assert "pnl" in segment
