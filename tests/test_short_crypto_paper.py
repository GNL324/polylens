from __future__ import annotations

import time
from typing import Any

from src.analysis.short_crypto_paper import (
    PaperConfig,
    ShortCryptoPaperStore,
    best_executable_price,
    build_intent,
    discover_markets,
    market_lead_time,
    performance_report,
    run_paper,
    settle_due,
    settle_trade,
    simulate_fill,
    validate_intent,
)
import src.analysis.short_crypto_paper as paper
from src.analysis.short_crypto_markets import ShortCryptoMarket


def _market(**overrides):
    now = time.time()
    data = {
        "asset": "BTC",
        "venue": "kalshi",
        "ticker": "KXBTC-TEST-B100",
        "start_ts": now - 60,
        "end_ts": now + 300,
        "direction": "down",
        "yes_bid": 0.4,
        "yes_ask": 0.45,
        "no_bid": 0.55,
        "no_ask": 0.6,
        "liquidity": 10.0,
        "window_minutes": 5,
        "strike_price": 100.0,
        "timestamp": now,
        "raw": {"strike_price": 100.0},
    }
    data.update(overrides)
    return ShortCryptoMarket(**data)


def _config(db_path):
    return PaperConfig(venues=["kalshi"], assets=["BTC"], windows=[5], max_trades=10, max_paper_exposure=10, min_edge=0.01, min_liquidity=1, freshness_seconds=60, db_path=str(db_path))


def test_paper_fill_uses_ask_for_buy():
    book = {"yes": {"bids": [{"price": 0.41, "size": 5}], "asks": [{"price": 0.46, "size": 5}]}}
    fill = simulate_fill({"side": "YES", "action": "BUY", "size": 2}, book)
    assert fill["filled"] is True
    assert fill["entry_price"] == 0.46
    assert fill["paper_cost"] == 0.92


def test_paper_fill_uses_bid_for_sell():
    book = {"yes": {"bids": [{"price": 0.42, "size": 5}], "asks": [{"price": 0.47, "size": 5}]}}
    best = best_executable_price(book, side="YES", action="SELL")
    assert best["price"] == 0.42


def test_no_fill_without_executable_book():
    assert simulate_fill({"side": "YES", "action": "BUY"}, {"yes": {"bids": [], "asks": []}})["filled"] is False


def test_duplicate_market_blocked(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    intent = build_intent(_market(), {"BTC": 100}.get("BTC"))
    intent.update(simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.01
    signal_id = store.save_signal(intent, run_id=run_id, status="accepted")
    store.save_trade(intent, signal_id=signal_id, run_id=run_id)
    assert validate_intent(intent, cfg, store, 0.0) == "duplicate_market"


def test_settlement_waits_until_end_time():
    future = time.time() + 120
    trade = {"end_time": _iso(future), "raw_json": "{}", "side": "YES", "direction": "down", "size": 1, "paper_cost": 0.5, "fee": 0}
    settlement = settle_trade(trade, now_ts=future - 1)
    assert settlement["result"] == "open"
    assert settlement["settlement_source"] == "not_past_end_time"
    assert settlement["reason"] == "end_time_not_passed"


def test_settlement_marks_expired_unsettled_when_source_missing():
    past = time.time() - 60
    trade = {"end_time": _iso(past), "raw_json": "{}", "side": "YES", "direction": "down", "size": 1, "paper_cost": 0.5, "fee": 0}
    settlement = settle_trade(trade, now_ts=time.time())
    assert settlement["result"] == "expired_unsettled"
    assert settlement["settlement_source"] == "unavailable"
    assert settlement["reason"] == "settlement_source_not_found"


def test_report_calculates_win_rate_and_roi(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    for won in [True, False]:
        intent = build_intent(_market(ticker=f"KXBTC-{won}"), 100.0)
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        trade = {"id": tid, **intent}
        trade["paper_cost"] = intent["paper_cost"]
        store.mark_settled(trade, {"result": "won" if won else "lost", "payout": 1.0 if won else 0.0, "pnl": (1.0 if won else 0.0) - intent["paper_cost"], "roi": ((1.0 if won else 0.0) - intent["paper_cost"]) / intent["paper_cost"], "settlement_source": "test"})
    report = performance_report(str(tmp_path / "paper.db"))
    assert report["closed_trades"] == 2
    assert report["win_rate"] == 0.5
    assert report["roi"] is not None


def test_report_status_accounting_sums_to_total(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()

    def _save_trade(ticker: str, *, end_offset: float, settle: dict[str, Any] | None = None):
        intent = build_intent(_market(ticker=ticker, end_ts=now + end_offset), 100.0)
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        if settle is not None:
            store.mark_settled({"id": tid, **intent, "paper_cost": intent["paper_cost"]}, settle)

    _save_trade("OPEN", end_offset=300)
    _save_trade("CLOSED-W", end_offset=-120, settle={"result": "won", "payout": 1.0, "pnl": 0.5, "roi": 1.0, "settlement_source": "test", "reason": "test"})
    _save_trade("CLOSED-L", end_offset=-120, settle={"result": "lost", "payout": 0.0, "pnl": -0.5, "roi": -1.0, "settlement_source": "test", "reason": "test"})
    _save_trade("EXPIRED", end_offset=-120, settle={"result": "expired_unsettled", "payout": None, "pnl": None, "roi": None, "settlement_source": "unavailable", "reason": "settlement_source_not_found"})
    _save_trade("PAST-DUE", end_offset=-120)

    report = performance_report(str(tmp_path / "paper.db"), now_ts=now)
    assert report["total_trades"] == 5
    assert report["open_trades"] == 1
    assert report["closed_trades"] == 2
    assert report["expired_unsettled_trades"] == 1
    assert report["canceled_future_market_trades"] == 0
    assert report["rejected_trades"] == 0
    assert report["other_trades"] == 1
    assert (
        report["open_trades"]
        + report["closed_trades"]
        + report["expired_unsettled_trades"]
        + report["canceled_future_market_trades"]
        + report["rejected_trades"]
        + report["other_trades"]
    ) == report["total_trades"]
    assert report["trades_by_status"]["open"] == 1
    assert report["trades_by_status"]["won"] == 1
    assert report["trades_by_status"]["lost"] == 1
    assert report["trades_by_status"]["expired_unsettled"] == 1
    assert report["trades_by_status"]["other"] == 1
    assert "unknown" not in report["trades_by_status"]


def test_legacy_unknown_reported_as_expired_unsettled(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()
    intent = build_intent(_market(ticker="UNKNOWN", end_ts=now - 120), 100.0)
    intent.update(simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
    store.mark_settled(
        {"id": tid, **intent, "paper_cost": intent["paper_cost"]},
        {"result": "unknown", "payout": None, "pnl": None, "roi": None, "settlement_source": "unavailable", "reason": "ambiguous_outcome"},
    )

    report = performance_report(str(tmp_path / "paper.db"), now_ts=now)
    assert report["expired_unsettled_trades"] == 1
    assert report["legacy_unknown_trades"] == 1
    assert "unknown" not in report["trades_by_status"]
    assert report["trades_by_status"]["expired_unsettled"] == 1
    assert len(report["sample_unsettled_trades"]) == 1
    sample = report["sample_unsettled_trades"][0]
    assert sample["venue"] == "kalshi"
    assert sample["status"] == "expired_unsettled"
    assert sample["reason"] == "settlement_source_not_found"


def test_live_db_status_shape_counts_legacy_unknown_and_canceled_future_market(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()

    def _seed_trade(idx: int, *, status: str, settle: dict[str, Any] | None = None):
        intent = build_intent(
            _market(ticker=f"btc-updown-5m-{idx}", venue="polymarket", raw={"slug": f"btc-updown-5m-{idx}"}, end_ts=now - 120),
            100.0,
        )
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        if settle is not None:
            store.mark_settled({"id": tid, **intent, "paper_cost": intent["paper_cost"]}, settle)
        with store.connect() as conn:
            conn.execute("UPDATE paper_trades SET status=? WHERE id=?", (status, tid))

    for idx in range(26):
        _seed_trade(idx, status="canceled_future_market")
    for idx in range(26, 44):
        _seed_trade(
            idx,
            status="unknown",
            settle={"result": "unknown", "payout": None, "pnl": None, "roi": None, "settlement_source": "unavailable", "reason": "settlement_source_not_found"},
        )

    report = performance_report(str(tmp_path / "paper.db"), now_ts=now)
    assert report["total_trades"] == 44
    assert report["closed_trades"] == 0
    assert report["expired_unsettled_trades"] == 18
    assert report["canceled_future_market_trades"] == 26
    assert report["open_trades"] == 0
    assert report["other_trades"] == 0
    assert report["legacy_unknown_trades"] == 18
    assert report["trades_by_status"] == {"expired_unsettled": 18, "canceled_future_market": 26}
    assert "unknown" not in report["trades_by_status"]
    assert sum(report["calibration_buckets"][bucket]["count"] for bucket in report["calibration_buckets"]) == 0
    assert any(sample["status"] == "canceled_future_market" for sample in report["sample_unsettled_trades"])
    assert any(sample["status"] == "expired_unsettled" for sample in report["sample_unsettled_trades"])
    assert all(sample["reason"] == "settlement_source_not_found" for sample in report["sample_unsettled_trades"] if sample["status"] == "expired_unsettled")
    assert all(sample["reason"] == "future_market_canceled" for sample in report["sample_unsettled_trades"] if sample["status"] == "canceled_future_market")
    assert (
        report["open_trades"]
        + report["closed_trades"]
        + report["expired_unsettled_trades"]
        + report["canceled_future_market_trades"]
        + report["rejected_trades"]
        + report["other_trades"]
    ) == report["total_trades"]


def test_calibration_excludes_open_and_unknown_trades(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()

    def _seed(ticker: str, *, prob: float, settle: dict[str, Any] | None):
        intent = build_intent(_market(ticker=ticker, end_ts=now + 300 if settle is None else now - 120), 100.0)
        intent["model_probability"] = prob
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        if settle is not None:
            store.mark_settled({"id": tid, **intent, "paper_cost": intent["paper_cost"]}, settle)

    _seed("OPEN", prob=0.73, settle=None)
    _seed("UNKNOWN", prob=0.73, settle={"result": "unknown", "payout": None, "pnl": None, "roi": None, "settlement_source": "unavailable", "reason": "ambiguous"})
    _seed("CANCELED", prob=0.73, settle=None)
    with store.connect() as conn:
        conn.execute("UPDATE paper_trades SET status='canceled_future_market' WHERE ticker=?", ("CANCELED",))
    _seed("CLOSED", prob=0.73, settle={"result": "won", "payout": 1.0, "pnl": 0.5, "roi": 1.0, "settlement_source": "test", "reason": "test"})

    report = performance_report(str(tmp_path / "paper.db"), now_ts=now)
    assert report["total_trades"] == 4
    assert report["expired_unsettled_trades"] == 1
    assert report["canceled_future_market_trades"] == 1
    assert report["calibration_buckets"]["70-80%"]["count"] == 1
    assert report["calibration_buckets"]["70-80%"]["observed_win_rate"] == 1.0


def test_report_warns_when_many_expired_unsettled_exist(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()
    for idx in range(3):
        intent = build_intent(_market(ticker=f"EXPIRED-{idx}", end_ts=now - 120), 100.0)
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        store.mark_settled(
            {"id": tid, **intent, "paper_cost": intent["paper_cost"]},
            {"result": "expired_unsettled", "payout": None, "pnl": None, "roi": None, "settlement_source": "unavailable", "reason": "settlement_source_not_found"},
        )

    report = performance_report(str(tmp_path / "paper.db"), now_ts=now)
    assert report["expired_unsettled_trades"] == 3
    assert any("expired_unsettled" in warning for warning in report["warnings"])


def test_calibration_buckets(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    intent = build_intent(_market(), 100.0)
    intent["model_probability"] = 0.73
    intent.update(simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
    store.mark_settled({"id": tid, **intent}, {"result": "won", "payout": 1.0, "pnl": 0.5, "roi": 1.0, "settlement_source": "test"})
    report = performance_report(str(tmp_path / "paper.db"))
    assert report["calibration_buckets"]["70-80%"]["count"] == 1
    assert report["calibration_buckets"]["70-80%"]["observed_win_rate"] == 1.0


def test_no_future_leakage_in_validation(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    intent = build_intent(_market(timestamp=time.time() + 30), 100.0)
    assert validate_intent(intent, cfg, store, 0.0) == "stale_order_book"


def test_rejected_signals_persisted(tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    intent = build_intent(_market(liquidity=0), 100.0)
    reason = validate_intent(intent, cfg, store, 0.0)
    signal_id = store.save_signal(intent, run_id=run_id, status="rejected", reason=reason)
    with store.connect() as conn:
        row = conn.execute("SELECT status, rejection_reason FROM paper_signals WHERE id=?", (signal_id,)).fetchone()
    assert row["status"] == "rejected"
    assert row["rejection_reason"] == "liquidity_below_threshold"


def test_paper_runner_uses_polymarket_series_discovery(tmp_path, monkeypatch):
    called = {"series": False}

    def fake_series(config, counters):
        called["series"] = True
        counters["polymarket_markets_seen"] += 1
        return [_market(venue="polymarket", ticker="btc-updown-5m-test", raw={"slug": "btc-updown-5m-test", "token_id": "123"})]

    monkeypatch.setattr(paper, "discover_polymarket_series_markets", fake_series)
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], db_path=str(tmp_path / "paper.db"), discover_only=True)
    counters = paper._debug_counters()
    markets = discover_markets(cfg, [], counters)
    assert called["series"] is True
    assert len(markets) == 1
    assert counters["polymarket_markets_seen"] == 1


def test_discover_only_does_not_create_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "discover_polymarket_series_markets", lambda config, counters: [_market(venue="polymarket", ticker="btc-updown-5m-test", raw={"slug": "btc-updown-5m-test", "token_id": "123"})])
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], db_path=str(tmp_path / "paper.db"), discover_only=True)
    result = run_paper(cfg)
    assert result["markets_seen"] == 1
    assert result["paper_trades_created"] == 0
    with ShortCryptoPaperStore(str(tmp_path / "paper.db")).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0


def test_min_edge_override_allows_pipeline_validation(tmp_path, monkeypatch):
    market = _market(venue="polymarket", ticker="btc-updown-5m-test", raw={"slug": "btc-updown-5m-test", "token_id": "123"})
    original_build_intent = paper.build_intent

    def negative_edge_intent(market, spot_price, now_ts=None):
        intent = original_build_intent(market, spot_price, now_ts=now_ts)
        intent["model_probability"] = 0.10
        intent["implied_probability"] = 0.80
        intent["edge"] = -0.70
        return intent

    monkeypatch.setattr(paper, "discover_polymarket_series_markets", lambda config, counters: [market])
    monkeypatch.setattr(paper, "fetch_spot_prices", lambda assets: {"BTC": 100.0})
    monkeypatch.setattr(paper, "build_intent", negative_edge_intent)
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], min_edge=-1.0, db_path=str(tmp_path / "paper.db"))
    result = run_paper(cfg)
    assert result["paper_trades_created"] == 1
    assert result["edge_blocks"] == 0


def test_rejection_counters_populate(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "discover_polymarket_series_markets", lambda config, counters: [_market(venue="polymarket", ticker="btc-updown-5m-test", raw={"slug": "btc-updown-5m-test", "token_id": "123"})])
    monkeypatch.setattr(paper, "fetch_spot_prices", lambda assets: {"BTC": 100.0})
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], min_edge=0.9, db_path=str(tmp_path / "paper.db"))
    result = run_paper(cfg)
    assert result["paper_trades_created"] == 0
    assert result["edge_blocks"] == 1
    assert result["markets_rejected_by_reason"]["edge_below_threshold"] == 1


def test_market_starting_in_5_minutes_is_accepted_by_lead_time_gate():
    now = time.time()
    timing = market_lead_time(_poly_row(now + 5 * 60, now + 10 * 60), now_ts=now)
    assert timing["reason"] is None
    assert 4.9 <= timing["lead_time_minutes"] <= 5.1


def test_market_starting_in_9_minutes_is_accepted_by_lead_time_gate():
    now = time.time()
    timing = market_lead_time(_poly_row(now + 9 * 60, now + 14 * 60), now_ts=now)
    assert timing["reason"] is None
    assert 8.9 <= timing["lead_time_minutes"] <= 9.1


def test_market_starting_in_45_minutes_with_60_minute_limit_is_accepted():
    now = time.time()
    timing = market_lead_time(_poly_row(now + 45 * 60, now + 50 * 60), now_ts=now, max_lead_time_minutes=60)
    assert timing["reason"] is None
    assert 44.9 <= timing["lead_time_minutes"] <= 45.1


def test_market_starting_in_61_minutes_with_60_minute_limit_is_rejected():
    now = time.time()
    timing = market_lead_time(_poly_row(now + 61 * 60, now + 66 * 60), now_ts=now, max_lead_time_minutes=60)
    assert timing["reason"] == "future_market"
    assert timing["lead_time_minutes"] > 60


def test_market_starting_in_11_minutes_with_10_minute_limit_is_rejected_as_future_market():
    now = time.time()
    timing = market_lead_time(_poly_row(now + 11 * 60, now + 16 * 60), now_ts=now, max_lead_time_minutes=10)
    assert timing["reason"] == "future_market"
    assert timing["lead_time_minutes"] > 10


def test_market_starting_in_2_hours_with_60_minute_limit_is_rejected_as_future_market():
    now = time.time()
    timing = market_lead_time(_poly_row(now + 120 * 60, now + 125 * 60), now_ts=now, max_lead_time_minutes=60)
    assert timing["reason"] == "future_market"
    assert timing["lead_time_minutes"] > 100


def test_paper_config_reads_max_market_lead_time_from_env(monkeypatch):
    monkeypatch.setenv("POLYLENS_MAX_MARKET_LEAD_TIME_MINUTES", "45")
    cfg = PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5])
    assert cfg.max_market_lead_time_minutes == 45.0


def _iso(ts):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _poly_row(start_ts, end_ts):
    return {
        "question": "Bitcoin Up or Down",
        "slug": "btc-updown-5m-test",
        "startTime": _iso(start_ts),
        "endDate": _iso(end_ts),
    }

def test_market_missing_start_time_is_rejected():
    now = time.time()
    # Market with end time but no start time
    row = {
        "question": "Bitcoin Up or Down",
        "slug": "btc-updown-5m-test",
        "endDate": _iso(now + 10 * 60),
    }
    timing = market_lead_time(row, now_ts=now)
    assert timing["reason"] == "missing_start_time"
    assert timing["lead_time_minutes"] is None
    assert timing["market_start_time"] is None
    assert timing["market_end_time"] is not None


def test_market_missing_end_time_but_has_start_time():
    now = time.time()
    # Market with start time but no end time - should be evaluated normally
    row = {
        "question": "Bitcoin Up or Down",
        "slug": "btc-updown-5m-test",
        "startTime": _iso(now + 5 * 60),
    }
    timing = market_lead_time(row, now_ts=now)
    # Should not be rejected for missing_start_time
    assert timing["reason"] is None
    assert 4.9 <= timing["lead_time_minutes"] <= 5.1
    assert timing["market_start_time"] is not None
    assert timing["market_end_time"] is None


def test_market_missing_both_start_and_end_time_is_rejected():
    now = time.time()
    # Market with neither start nor end time
    row = {
        "question": "Bitcoin Up or Down",
        "slug": "btc-updown-5m-test",
    }
    timing = market_lead_time(row, now_ts=now)
    assert timing["reason"] == "missing_start_time"
    assert timing["lead_time_minutes"] is None
    assert timing["market_start_time"] is None
    assert timing["market_end_time"] is None


def test_market_normal_valid_market_still_accepted():
    now = time.time()
    # Normal market with both start and end times
    row = _poly_row(now + 5 * 60, now + 10 * 60)
    timing = market_lead_time(row, now_ts=now)
    assert timing["reason"] is None
    assert 4.9 <= timing["lead_time_minutes"] <= 5.1
    assert timing["market_start_time"] is not None
    assert timing["market_end_time"] is not None
