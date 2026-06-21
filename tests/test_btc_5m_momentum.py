from __future__ import annotations

import json
import sys
import time

import pytest

from src.analysis.btc_5m_momentum import (
    Btc5mMomentumConfig,
    BtcSpotSnapshot,
    evaluate_momentum_pair,
    run_btc_5m_momentum,
    select_stronger_side,
)
from src.analysis.short_crypto_markets import ShortCryptoMarket
from src.analysis.short_crypto_paper import ShortCryptoPaperStore


def _market(direction: str, **overrides) -> ShortCryptoMarket:
    now = time.time()
    data = {
        "asset": "BTC",
        "venue": "polymarket",
        "ticker": "btc-updown-5m-test",
        "start_ts": now - 180,
        "end_ts": now + 90,
        "direction": direction,
        "yes_bid": 0.62,
        "yes_ask": 0.64,
        "no_bid": None,
        "no_ask": None,
        "liquidity": 20.0,
        "window_minutes": 5,
        "timestamp": now,
        "raw": {"slug": f"btc-updown-5m-test-{direction}", "token_id": f"token-{direction}"},
    }
    data.update(overrides)
    return ShortCryptoMarket(**data)


def _pair(**overrides):
    up_overrides = overrides.pop("up", {})
    down_overrides = overrides.pop("down", {})
    return {"up": _market("up", **up_overrides), "down": _market("down", **down_overrides)}


def _spot(**overrides):
    data = {"bucket_open": 100000.0, "current": 100250.0, "timestamp": time.time(), "source": "test"}
    data.update(overrides)
    return BtcSpotSnapshot(**data)


def _config(**overrides):
    data = {
        "entry_window_sec": 120.0,
        "exit_before_sec": 15.0,
        "max_spread": 0.05,
        "stale_quote_sec": 30.0,
        "min_liquidity": 5.0,
        "min_side_price": 0.60,
        "min_impulse_abs": 50.0,
        "min_impulse_pct": 0.001,
    }
    data.update(overrides)
    return Btc5mMomentumConfig(**data)


def test_stronger_side_selection_prefers_higher_crossed_side():
    up = _market("up", yes_ask=0.66)
    down = _market("down", yes_ask=0.61)

    selected, side, opposite = select_stronger_side(up, down, min_side_price=0.60)

    assert side == "up"
    assert selected is up
    assert opposite is down


def test_both_sides_above_threshold_selects_stronger_down_side():
    pair = _pair(up={"yes_ask": 0.62}, down={"yes_bid": 0.66, "yes_ask": 0.68})
    decision = evaluate_momentum_pair(
        pair,
        _spot(current=99700.0),
        config=_config(min_impulse_abs=100.0, min_impulse_pct=0.001),
        now_ts=time.time(),
    )

    assert decision["trade"] is True
    assert decision["intent"]["selected_side"] == "down"
    assert decision["intent"]["opposite_side_price"] == 0.62


def test_no_trade_when_impulse_not_confirmed():
    decision = evaluate_momentum_pair(
        _pair(up={"yes_ask": 0.66}, down={"yes_ask": 0.61}),
        _spot(current=100020.0),
        config=_config(min_impulse_abs=50.0, min_impulse_pct=0.001),
        now_ts=time.time(),
    )

    assert decision["trade"] is False
    assert decision["skip_reason"] == "impulse_not_confirmed"


def test_no_trade_when_spot_stale():
    now = time.time()
    decision = evaluate_momentum_pair(
        _pair(),
        _spot(timestamp=now - 100),
        config=_config(stale_quote_sec=10.0),
        now_ts=now,
    )

    assert decision["trade"] is False
    assert decision["skip_reason"] == "spot_stale"


def test_no_trade_when_quote_stale():
    now = time.time()
    decision = evaluate_momentum_pair(
        _pair(up={"timestamp": now - 100}),
        _spot(timestamp=now),
        config=_config(stale_quote_sec=10.0),
        now_ts=now,
    )

    assert decision["trade"] is False
    assert decision["skip_reason"] == "quote_stale"


def test_no_trade_when_spread_too_wide():
    decision = evaluate_momentum_pair(
        _pair(up={"yes_bid": 0.50, "yes_ask": 0.66}),
        _spot(),
        config=_config(max_spread=0.05),
        now_ts=time.time(),
    )

    assert decision["trade"] is False
    assert decision["skip_reason"] == "spread_too_wide"


def test_no_trade_when_liquidity_too_low():
    decision = evaluate_momentum_pair(
        _pair(up={"liquidity": 1.0}),
        _spot(),
        config=_config(min_liquidity=5.0),
        now_ts=time.time(),
    )

    assert decision["trade"] is False
    assert decision["skip_reason"] == "liquidity_too_low"


def test_paper_trade_metadata_is_recorded(tmp_path):
    cfg = _config(db_path=str(tmp_path / "paper.db"), fixed_paper_size=2.0)
    result = run_btc_5m_momentum(
        cfg,
        market_provider=lambda _cfg: [_market("up", yes_bid=0.64, yes_ask=0.66), _market("down", yes_ask=0.61)],
        spot_provider=lambda: _spot(current=100250.0),
        now_ts=time.time(),
    )

    assert result["paper_trades_created"] == 1
    with ShortCryptoPaperStore(str(tmp_path / "paper.db")).connect() as conn:
        row = conn.execute("SELECT strategy_label, raw_json FROM paper_trades").fetchone()
    payload = json.loads(row["raw_json"])
    assert row["strategy_label"] == "btc_5m_momentum_close"
    assert payload["strategy_label"] == "btc_5m_momentum_close"
    assert payload["selected_side"] == "up"
    assert payload["opposite_side_price"] == 0.61
    assert payload["seconds_to_expiry"] > 0
    assert payload["spread"] == pytest.approx(0.02)
    assert payload["liquidity"] == 20.0
    assert payload["btc_bucket_open"] == 100000.0
    assert payload["btc_current"] == 100250.0
    assert payload["btc_impulse_abs"] == 250.0
    assert payload["btc_impulse_pct"] == pytest.approx(0.0025)
    assert payload["impulse_confirmed"] is True
    assert payload["skip_reason"] is None
    assert payload["would_exit_before_expiry"] is True
    assert payload["would_force_close"] is False
    assert payload["exit_reason"] == "exit_before_expiry_scheduled"


def test_live_mode_is_rejected():
    with pytest.raises(ValueError, match="paper-only"):
        run_btc_5m_momentum(_config(live=True, paper=False))


def test_cli_live_mode_is_rejected(capsys, monkeypatch):
    from src.cli import main

    monkeypatch.setattr(sys, "argv", ["polylens", "watch-btc-5m-momentum", "--live", "--json"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["paper_only"] is True
    assert "paper-only" in payload["error"]
