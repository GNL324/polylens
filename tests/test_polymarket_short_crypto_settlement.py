from __future__ import annotations

import time
from typing import Any

import src.analysis.short_crypto_paper as paper
from src.analysis.polymarket_short_crypto_settlement import (
    compute_paper_trade_pnl,
    resolve_polymarket_short_crypto_market,
    settlement_audit,
    trade_outcome_to_result,
)
from src.analysis.short_crypto_paper import (
    PaperConfig,
    ShortCryptoPaperStore,
    build_intent,
    performance_report,
    settle_due,
    settle_trade,
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
        "end_ts": now - 120,
        "direction": "up",
        "yes_bid": 0.4,
        "yes_ask": 0.51,
        "no_bid": 0.49,
        "no_ask": 0.6,
        "liquidity": 10.0,
        "window_minutes": 5,
        "timestamp": now,
        "raw": {"slug": "btc-updown-5m-test", "token_id": "123"},
    }
    data.update(overrides)
    return ShortCryptoMarket(**data)


def _config(db_path):
    return PaperConfig(venues=["polymarket"], assets=["BTC"], windows=[5], db_path=str(db_path))


def test_compute_paper_trade_pnl_buy_yes_win_and_loss():
    win = compute_paper_trade_pnl(entry_price=0.51, size=1.0, won=True)
    loss = compute_paper_trade_pnl(entry_price=0.51, size=1.0, won=False)
    assert win["paper_cost"] == 0.51
    assert win["payout"] == 1.0
    assert round(win["pnl"], 2) == 0.49
    assert loss["payout"] == 0.0
    assert round(loss["pnl"], 2) == -0.51


def test_resolve_polymarket_short_crypto_market_from_gamma(monkeypatch):
    monkeypatch.setattr(
        "src.analysis.polymarket_short_crypto_settlement.resolve_gamma_event_slug",
        lambda slug: {
            "resolved_market": {
                "umaResolutionStatus": "resolved",
                "outcomes": '["Up", "Down"]',
                "outcomePrices": '["0", "1"]',
                "closedTime": "2026-06-07 23:00:16+00",
            },
            "resolved_event": {
                "automaticallyResolved": True,
                "eventMetadata": {"finalPrice": 62838.31, "priceToBeat": 63000.15},
                "closedTime": "2026-06-07T23:00:16Z",
            },
        },
    )
    result = resolve_polymarket_short_crypto_market("btc-updown-5m-1780872900", include_diagnostics=True)
    assert result["resolved"] is True
    assert result["outcome"] == "Down"
    assert result["source"] == "gamma_outcome_prices"
    assert result["confidence"] == 1.0
    assert any(item["source"] == "gamma_outcome_prices" for item in result["diagnostics"])


def test_resolve_polymarket_short_crypto_market_unresolved(monkeypatch):
    monkeypatch.setattr(
        "src.analysis.polymarket_short_crypto_settlement.resolve_gamma_event_slug",
        lambda slug: {"resolved_market": {"umaResolutionStatus": "pending"}, "resolved_event": {}},
    )
    result = resolve_polymarket_short_crypto_market("btc-updown-5m-missing")
    assert result["resolved"] is False
    assert result["reason"] == "outcome_not_available"


def test_trade_outcome_to_result_matches_direction():
    trade = {"direction": "up"}
    assert trade_outcome_to_result(trade, "Up") == "won"
    assert trade_outcome_to_result(trade, "Down") == "lost"


def test_settle_trade_resolves_polymarket_expired_unsettled(monkeypatch, tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    intent = build_intent(_market(direction="down"), 100.0)
    intent.update(simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
    with store.connect() as conn:
        conn.execute("UPDATE paper_trades SET status='unknown' WHERE id=?", (tid,))

    monkeypatch.setattr(
        "src.analysis.polymarket_short_crypto_settlement.resolve_polymarket_short_crypto_market",
        lambda slug: {
            "resolved": True,
            "outcome": "Down",
            "source": "gamma_outcome_prices",
            "confidence": 1.0,
            "settlement_timestamp": "2026-06-07T23:00:16Z",
        },
    )
    trade = {"id": tid, **intent, "paper_cost": intent["paper_cost"], "fee": 0.0, "action": "BUY"}
    settlement = settle_trade(trade, now_ts=time.time())
    assert settlement["result"] == "won"
    assert settlement["settlement_source"] == "gamma_outcome_prices"
    assert round(settlement["pnl"], 2) == 0.49


def test_settle_due_reprocesses_unknown_and_canceled_future_market(monkeypatch, tmp_path):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)

    def _seed(status: str, direction: str):
        intent = build_intent(_market(direction=direction, ticker=f"btc-{status}"), 100.0)
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        with store.connect() as conn:
            conn.execute("UPDATE paper_trades SET status=? WHERE id=?", (status, tid))

    _seed("unknown", "down")
    _seed("canceled_future_market", "up")

    monkeypatch.setattr(
        "src.analysis.polymarket_short_crypto_settlement.resolve_polymarket_short_crypto_market",
        lambda slug: {
            "resolved": True,
            "outcome": "Down" if "unknown" in slug else "Up",
            "source": "gamma_outcome_prices",
            "confidence": 1.0,
        },
    )
    result = settle_due(str(tmp_path / "paper.db"))
    assert result["due_trades"] == 2
    assert result["resolved"] == 2
    assert result["remaining_unsettled"] == 0


def test_live_db_shape_legacy_unknown_and_canceled_future_market_reporting(tmp_path, monkeypatch):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()

    def _seed(idx: int, *, status: str, direction: str):
        intent = build_intent(
            _market(
                ticker=f"btc-updown-5m-{idx}",
                direction=direction,
                raw={"slug": f"btc-updown-5m-{idx}"},
                end_ts=now - 120,
            ),
            100.0,
        )
        intent.update(simulate_fill(intent, intent["book"]))
        intent["expected_value"] = 0.05
        sid = store.save_signal(intent, run_id=run_id, status="accepted")
        tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
        with store.connect() as conn:
            conn.execute("UPDATE paper_trades SET status=? WHERE id=?", (status, tid))

    for idx in range(26):
        _seed(idx, status="canceled_future_market", direction="up")
    for idx in range(26, 44):
        _seed(idx, status="unknown", direction="down")

    monkeypatch.setattr(
        "src.analysis.polymarket_short_crypto_settlement.resolve_polymarket_short_crypto_market",
        lambda slug: {
            "resolved": True,
            "outcome": "Down",
            "source": "gamma_outcome_prices",
            "confidence": 1.0,
        },
    )
    settle_due(str(tmp_path / "paper.db"), now_ts=now)
    report = performance_report(str(tmp_path / "paper.db"), now_ts=now)
    assert report["total_trades"] == 44
    assert report["closed_trades"] == 44
    assert report["won_trades"] == 18
    assert report["lost_trades"] == 26
    assert report["expired_unsettled_trades"] == 0
    assert report["canceled_future_market_trades"] == 0
    assert sum(report["calibration_buckets"][bucket]["count"] for bucket in report["calibration_buckets"]) == 44
    assert report["realized_pnl"] is not None
    assert report["expectancy"] is not None


def test_settlement_audit_counts_resolved_and_remaining(tmp_path, monkeypatch):
    store = ShortCryptoPaperStore(str(tmp_path / "paper.db"))
    cfg = _config(tmp_path / "paper.db")
    run_id = store.start_run(cfg)
    now = time.time()
    intent = build_intent(_market(end_ts=now - 60), 100.0)
    intent.update(simulate_fill(intent, intent["book"]))
    intent["expected_value"] = 0.05
    sid = store.save_signal(intent, run_id=run_id, status="accepted")
    tid = store.save_trade(intent, signal_id=sid, run_id=run_id)
    with store.connect() as conn:
        conn.execute("UPDATE paper_trades SET status='unknown' WHERE id=?", (tid,))

    monkeypatch.setattr(
        "src.analysis.polymarket_short_crypto_settlement.resolve_polymarket_short_crypto_market",
        lambda slug, include_diagnostics=False: {
            "resolved": True,
            "outcome": "Down",
            "source": "gamma_outcome_prices",
            "confidence": 1.0,
            "diagnostics": [{"source": "gamma_outcome_prices", "resolved": True}] if include_diagnostics else None,
        },
    )
    audit = settlement_audit(str(tmp_path / "paper.db"))
    assert audit["expired_unsettled"] == 1
    assert audit["resolved"] == 1
    assert audit["remaining"] == 0
    assert audit["sources_tested"]
