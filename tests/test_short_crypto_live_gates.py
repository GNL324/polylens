from __future__ import annotations

import time
from datetime import datetime, timezone

from src.analysis.short_crypto_executor import (
    CryptoSignal,
    KalshiShortCryptoLiveAdapter,
    PolymarketShortCryptoLiveAdapter,
    ShortCryptoExecutor,
    ShortCryptoRiskConfig,
    _dedupe_key,
    build_kalshi_order_payload,
    build_kalshi_order_semantics,
    first_live_test_dedupe_context,
    live_trade_dedupe_key,
    select_executable_yes_ask,
)
from src.analysis.short_crypto_markets import ShortCryptoMarket


def _signal(**overrides):
    now = time.time()
    market = ShortCryptoMarket(
        asset="BTC",
        venue=overrides.pop("venue", "kalshi"),
        ticker=overrides.pop("ticker", f"BTC-UP-5-{now}"),
        start_ts=now,
        end_ts=overrides.pop("end_ts", now + 300),
        direction="up",
        yes_bid=overrides.pop("yes_bid", 0.5),
        yes_ask=overrides.pop("yes_ask", 0.55),
        no_bid=0.45,
        no_ask=0.5,
        liquidity=overrides.pop("liquidity", 100.0),
        window_minutes=5,
        timestamp=overrides.pop("book_timestamp", now),
    )
    meta = {"market": market, "price_timestamp": overrides.pop("price_timestamp", now)}
    return CryptoSignal(
        asset="BTC",
        window_minutes=5,
        direction="up",
        venue=market.venue,
        ticker=market.ticker,
        spot_price=100.0,
        implied_prob=0.525,
        model_prob=0.6,
        edge=0.075,
        roi=0.1,
        timestamp=overrides.pop("timestamp", now),
        meta=meta,
    )


def _executor(tmp_path, **config_overrides):
    config = ShortCryptoRiskConfig(
        max_trade_stake=10.0,
        max_trade_stake_cap=25.0,
        max_daily_loss=100.0,
        max_daily_notional=100.0,
        max_open_notional=100.0,
        max_trades_per_day=10,
        max_per_venue_notional=100.0,
        min_edge=0.01,
        min_liquidity=1.0,
        data_freshness_seconds=2.0,
        book_freshness_seconds=2.0,
        close_cutoff_seconds=20.0,
        duplicate_trade_protection=True,
        kill_switch=str(tmp_path / ".kill_switch"),
    )
    config = ShortCryptoRiskConfig(**{**config.__dict__, **config_overrides})
    return ShortCryptoExecutor(config=config, db_path=str(tmp_path / "trades.db"))


def test_live_blocked_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYLENS_LIVE_TRADING", raising=False)
    res = _executor(tmp_path).execute(_signal(), mode="live", live=True)
    assert res["accepted"] is False
    assert res["reason"] == "missing_live_gate_polylens_live_trading"


def test_live_blocked_without_all_env_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.delenv("POLYLENS_CONFIRM_RISK_ACK", raising=False)
    res = _executor(tmp_path).execute(_signal(), mode="live", live=True)
    assert res["reason"] == "missing_live_gate_polylens_confirm_risk_ack"


def test_live_blocked_if_kill_switch_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    (tmp_path / ".kill_switch").write_text("stop", encoding="utf-8")
    res = _executor(tmp_path).execute(_signal(), mode="live", live=True)
    assert res["reason"] == "kill_switch_active"


def test_live_blocked_on_stale_price_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    res = _executor(tmp_path).execute(_signal(price_timestamp=time.time() - 10), mode="live", live=True)
    assert res["reason"] == "stale_price_feed"


def test_live_blocked_on_stale_order_book(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    res = _executor(tmp_path).execute(_signal(book_timestamp=time.time() - 10), mode="live", live=True)
    assert res["reason"] == "stale_order_book"


def test_live_blocked_on_duplicate_trade_key(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    ex = _executor(tmp_path)
    sig = _signal(venue="polymarket")
    first = ex.execute(sig, mode="live", live=True)
    second = ex.execute(sig, mode="live", live=True)
    assert first["reason"] == "polymarket_live_execution_not_implemented"
    assert second["reason"] == "duplicate_trade_key"


def test_live_blocked_when_daily_loss_exceeded(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    ex = _executor(tmp_path)
    ex._state.day_key = datetime.now(timezone.utc).date().isoformat()
    ex._state.daily_loss = 100.0
    res = ex.execute(_signal(), mode="live", live=True)
    assert res["reason"] == "daily_loss_limit_breached"


def test_paper_trade_succeeds_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    res = _executor(tmp_path).execute(_signal(), mode="paper")
    assert res["accepted"] is True
    assert res["mode"] == "paper"


def test_polymarket_live_returns_blocked_if_signing_incomplete():
    res = PolymarketShortCryptoLiveAdapter().submit({"venue": "polymarket", "ticker": "x"})
    assert res["status"] == "blocked"
    assert res["reason"] == "polymarket_live_execution_not_implemented"


def test_kalshi_live_sends_are_stopped_for_semantics_audit(monkeypatch):
    monkeypatch.delenv("POLYLENS_KALSHI_LIVE_SENDS_ENABLED", raising=False)
    res = KalshiShortCryptoLiveAdapter().submit({"venue": "kalshi", "ticker": "x", "kalshi_payload": {"ticker": "x"}})
    assert res["status"] == "blocked"
    assert res["reason"] == "kalshi_live_sends_stopped_for_order_semantics_audit"


def test_first_live_live_missing_run_id_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST", "true")
    monkeypatch.delenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", raising=False)
    res = _executor(tmp_path).execute(_signal(), mode="live", live=True, max_loops=1)
    assert res["reason"] == "missing_first_live_test_run_id"


def test_first_live_run_id_only_applies_to_live_one_loop_one_count(monkeypatch):
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    context = first_live_test_dedupe_context(mode="live", count=1, max_loops=1)
    assert context == {"enabled": True, "run_id": "test-001", "missing_run_id": False}
    assert first_live_test_dedupe_context(mode="paper", count=1, max_loops=1)["enabled"] is False
    assert first_live_test_dedupe_context(mode="live", count=2, max_loops=1)["enabled"] is False
    assert first_live_test_dedupe_context(mode="live", count=1, max_loops=2)["enabled"] is False


def test_first_live_run_id_changes_live_dedupe_key(monkeypatch):
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    sig = _signal(ticker="KXBTC-RUN-ID")
    old_key = _dedupe_key(sig.asset, sig.window_minutes, sig.direction, sig.venue, sig.ticker, "live")
    run_key = live_trade_dedupe_key(sig, mode="live", count=1, max_loops=1)
    assert run_key != old_key
    assert live_trade_dedupe_key(sig, mode="live", count=1, max_loops=2) == old_key


def test_first_live_same_run_id_still_blocks_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYLENS_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYLENS_AUTONOMOUS_CRYPTO", "true")
    monkeypatch.setenv("POLYLENS_CONFIRM_RISK_ACK", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST", "true")
    monkeypatch.setenv("POLYLENS_FIRST_LIVE_TEST_RUN_ID", "test-001")
    ex = _executor(tmp_path)
    sig = _signal(ticker="KXBTC-RUN-ID")
    first = ex.execute(sig, mode="live", live=True, max_loops=1)
    second = ex.execute(sig, mode="live", live=True, max_loops=1)
    assert first["reason"] == "kalshi_live_sends_stopped_for_order_semantics_audit"
    assert second["reason"] == "duplicate_trade_key"


def test_kalshi_order_payload_generation_is_correct():
    payload = build_kalshi_order_payload(
        {
            "ticker": "BTC-UP-5",
            "side": "yes",
            "action": "buy",
            "price": 0.55,
            "count": 3,
            "stake": 1.65,
            "client_order_id": "client-1",
        }
    )
    assert payload == {
        "ticker": "BTC-UP-5",
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": 3,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "client-1",
        "buy_max_cost": 165,
        "yes_price": 55,
    }


def test_no_executable_yes_ask_blocks_first_live_post():
    book = {"orderbook_fp": {"no_dollars": [["0.0100", "10.00"]], "yes_dollars": []}}
    selected = select_executable_yes_ask(book)
    assert selected["ok"] is True
    assert selected["price_cents"] == 99
    assert selected["count"] == 10
    assert selected["derived_from"] == "no_bid"


def test_yes_bid_is_not_treated_as_yes_ask():
    book = {"orderbook_fp": {"yes_dollars": [["0.0200", "10.00"]], "no_dollars": []}}
    selected = select_executable_yes_ask(book)
    assert selected == {"ok": False, "reason": "no_executable_resting_yes_ask"}


def test_no_no_bid_means_no_executable_yes_ask():
    book = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
    selected = select_executable_yes_ask(book)
    assert selected == {"ok": False, "reason": "no_executable_resting_yes_ask"}


def test_live_selection_uses_derived_yes_ask_from_no_bid():
    book = {"orderbook_fp": {"no_dollars": [["0.9800", "3.00"]], "yes_dollars": [["0.5000", "99.00"]]}}
    selected = select_executable_yes_ask(book)
    assert selected["price_cents"] == 2
    assert selected["count"] == 3
    assert selected["raw"] == ["0.9800", "3.00"]


def test_kalshi_semantics_buy_yes_direct_candidate_uses_derived_yes_ask():
    book = {"orderbook_fp": {"no_dollars": [["0.9800", "4.00"]], "yes_dollars": []}}
    semantics = build_kalshi_order_semantics("KXBTC-TEST", book)
    assert semantics["derived_yes_ask_from_no_bid"] == {"cents": 2, "dollars": 0.02}
    assert semantics["candidate_payloads"]["A"] == {
        "ticker": "KXBTC-TEST",
        "side": "yes",
        "action": "buy",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "polylens-sem-buy-yes",
        "buy_max_cost": 2,
        "yes_price": 2,
    }


def test_kalshi_semantics_sell_no_crosses_resting_no_bid_without_buy_max_cost():
    book = {"orderbook_fp": {"no_dollars": [["0.9800", "4.00"]], "yes_dollars": []}}
    semantics = build_kalshi_order_semantics("KXBTC-TEST", book)
    assert semantics["candidate_payloads"]["D"] == {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "sell",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "polylens-sem-sell-no",
        "no_price": 98,
    }
    assert "buy_max_cost" not in semantics["candidate_payloads"]["D"]


def test_kalshi_semantics_sell_yes_crosses_resting_yes_bid():
    book = {"orderbook_fp": {"yes_dollars": [["0.1100", "7.00"]], "no_dollars": []}}
    semantics = build_kalshi_order_semantics("KXBTC-TEST", book)
    assert semantics["derived_no_ask_from_yes_bid"] == {"cents": 89, "dollars": 0.89}
    assert semantics["candidate_payloads"]["B"] == {
        "ticker": "KXBTC-TEST",
        "side": "yes",
        "action": "sell",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "polylens-sem-sell-yes",
        "yes_price": 11,
    }


def test_kalshi_semantics_buy_no_direct_candidate_uses_derived_no_ask():
    book = {"orderbook_fp": {"yes_dollars": [["0.1100", "7.00"]], "no_dollars": []}}
    semantics = build_kalshi_order_semantics("KXBTC-TEST", book)
    assert semantics["candidate_payloads"]["C"] == {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "buy",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "polylens-sem-buy-no",
        "buy_max_cost": 89,
        "no_price": 89,
    }


def test_first_live_no_bid_ladder_payload_is_sell_no_without_buy_max_cost():
    selected = {"derived_from": "no_bid", "no_bid_cents": 99, "price_cents": 1, "count": 10}
    payload = build_kalshi_order_payload(
        {
            "ticker": "KXBTC-TEST",
            "side": "no",
            "action": "sell",
            "price": 0.01,
            "no_price_cents": selected["no_bid_cents"],
            "count": 1,
            "client_order_id": "client-1",
        }
    )
    assert payload == {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "sell",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "client-1",
        "no_price": 99,
    }


def test_live_order_audit_validation_rejects_sell_buy_max_cost():
    from live_order_audit import _validate_payload

    selected = {"derived_from": "no_bid", "no_bid_cents": 99, "price_cents": 1, "count": 10}
    payload = {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "sell",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "client-1",
        "no_price": 99,
        "buy_max_cost": 1,
    }
    validation = _validate_payload(payload, first_live_test=True, stake=1.0, price=0.01, selected=selected)
    assert validation["ok"] is False
    assert "sell_payload_must_not_include_buy_max_cost" in validation["problems"]


def test_live_order_audit_validation_requires_sell_no_price():
    from live_order_audit import _validate_payload

    selected = {"derived_from": "no_bid", "no_bid_cents": 99, "price_cents": 1, "count": 10}
    payload = {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "sell",
        "type": "limit",
        "count": 1,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "client-1",
    }
    validation = _validate_payload(payload, first_live_test=True, stake=1.0, price=0.01, selected=selected)
    assert validation["ok"] is False
    assert "sell_no_into_no_bid_must_include_no_price" in validation["problems"]


def test_live_order_audit_validation_rejects_first_live_count_gt_one():
    from live_order_audit import _validate_payload

    selected = {"derived_from": "no_bid", "no_bid_cents": 99, "price_cents": 1, "count": 10}
    payload = {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "sell",
        "type": "limit",
        "count": 2,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "client-1",
        "no_price": 99,
    }
    validation = _validate_payload(payload, first_live_test=True, stake=1.0, price=0.01, selected=selected)
    assert validation["ok"] is False
    assert "first_live_test_reject_count_gt_1" in validation["problems"]


def test_live_order_audit_validation_rejects_first_live_exposure_gt_one_dollar():
    from live_order_audit import _validate_payload

    selected = {"derived_from": "no_bid", "no_bid_cents": 1, "price_cents": 99, "count": 10}
    payload = {
        "ticker": "KXBTC-TEST",
        "side": "no",
        "action": "sell",
        "type": "limit",
        "count": 2,
        "time_in_force": "immediate_or_cancel",
        "client_order_id": "client-1",
        "no_price": 1,
    }
    validation = _validate_payload(payload, first_live_test=True, stake=1.0, price=0.99, selected=selected)
    assert validation["ok"] is False
    assert "first_live_test_equivalent_exposure_gt_1_dollar" in validation["problems"]
