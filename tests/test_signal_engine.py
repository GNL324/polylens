from __future__ import annotations

import json
import time
from pathlib import Path

from src.analysis.trader_registry import save_wallet_report
from src.analysis.trader_signal_engine import init_trader_signal_db, persist_signals
from src.intelligence.signal_engine import SignalEngine, SignalEngineConfig, run_wallet_intelligence_cycle
from src.intelligence.strategy_classifier import StrategyClassifier
from src.intelligence.wallet_tracker import WalletTracker

WALLET = "0x" + "a1" * 20


def _report():
    return {
        "wallet": WALLET,
        "classification": "quantitative_directional",
        "confidence": 0.85,
        "watch_score": 75,
        "metrics": {
            "trade_count": 40,
            "markets_traded": 12,
            "overlap_count": 4,
            "overlap_ratio": 0.25,
            "merge_count": 2,
            "redeem_count": 1,
            "buy_volume": 800,
            "sell_volume": 200,
            "btc_volume": 600,
            "eth_volume": 200,
            "sol_volume": 0,
        },
        "signals": [],
    }


def _event(action: str = "buy", **overrides):
    row = {
        "wallet": WALLET,
        "timestamp": time.time(),
        "event_type": action,
        "market_id": "market-1",
        "condition_id": "condition-1",
        "market_slug": "btc-up",
        "market_title": "BTC Up",
        "asset": "BTC",
        "side": "up",
        "action": action,
        "shares": 20.0,
        "price": 0.55,
        "amount": 11.0,
        "tx_hash": f"0x{action}-1",
    }
    row.update(overrides)
    return row


def _activity_export(events, wallet: str = WALLET):
    return {
        "wallet": wallet,
        "export_timestamp": "2026-06-15T00:00:00Z",
        "source": "test",
        "event_count": len(events),
        "events": events,
    }


def test_filter_signals_rejects_stale_and_duplicate(tmp_path):
    signal_db = tmp_path / "signals.db"
    init_trader_signal_db(signal_db)

    engine = SignalEngine(signal_db_path=signal_db, config=SignalEngineConfig(max_signal_age_seconds=3600))
    stale_signal = {
        "wallet": WALLET,
        "market_id": "market-1",
        "side": "up",
        "timestamp": time.time() - 10_000,
        "amount": 10.0,
        "shares": 5.0,
        "signal_type": "early_entry",
        "signal_key": "stale",
    }
    fresh_signal = {
        "wallet": WALLET,
        "market_id": "market-2",
        "side": "up",
        "timestamp": time.time(),
        "amount": 10.0,
        "shares": 5.0,
        "signal_type": "conviction",
        "signal_key": "fresh",
    }
    duplicate_signal = {
        "wallet": WALLET,
        "market_id": "market-3",
        "side": "up",
        "timestamp": time.time(),
        "amount": 10.0,
        "shares": 5.0,
        "signal_type": "conviction",
        "signal_key": "dup",
    }

    persist_signals(
        [
            {
                **duplicate_signal,
                "market_title": "BTC Up",
                "asset": "BTC",
                "action": "buy",
                "price": 0.5,
                "tx_hash": "0xdup",
                "reason": "test",
            }
        ],
        db_path=signal_db,
    )

    accepted, rejected = engine.filter_signals([stale_signal, fresh_signal, duplicate_signal])
    assert rejected["stale"] == 1
    assert rejected["duplicate"] == 1
    assert len(accepted) == 1
    assert accepted[0]["signal_key"] == "fresh"


def test_generate_wallet_signals_from_export(tmp_path):
    traders_db = tmp_path / "traders.db"
    signal_db = tmp_path / "signals.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(), db_path=str(traders_db))
    activity_path = export_dir / f"{WALLET}_activity.json"
    activity_path.write_text(json.dumps(_activity_export([_event("buy", amount=120.0)])), encoding="utf-8")

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=export_dir,
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    tracker.persist_watchlist()

    engine = SignalEngine(wallet_tracker=tracker, signal_db_path=signal_db)
    result = engine.generate_wallet_signals()

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["events_loaded"] >= 1
    assert result["signals_accepted"] >= 1


def test_run_signal_cycle_integrates_recommendations(tmp_path):
    traders_db = tmp_path / "traders.db"
    signal_db = tmp_path / "signals.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(), db_path=str(traders_db))
    activity_path = export_dir / f"{WALLET}_activity.json"
    activity_path.write_text(json.dumps(_activity_export([_event("buy", amount=200.0)])), encoding="utf-8")

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=export_dir,
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    engine = SignalEngine(wallet_tracker=tracker, signal_db_path=signal_db)
    cycle = engine.run_signal_cycle(activity_paths=[activity_path])

    assert cycle["paper_only"] is True
    assert cycle["generation"]["signals_accepted"] >= 1
    assert cycle["recommendations"] is not None


def test_integrate_paper_trading_creates_intents(tmp_path):
    traders_db = tmp_path / "traders.db"
    signal_db = tmp_path / "signals.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(), db_path=str(traders_db))
    activity_path = export_dir / f"{WALLET}_activity.json"
    activity_path.write_text(json.dumps(_activity_export([_event("buy", amount=200.0)])), encoding="utf-8")

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=export_dir,
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    engine = SignalEngine(wallet_tracker=tracker, signal_db_path=signal_db)
    engine.run_signal_cycle(activity_paths=[activity_path])
    bridge = engine.integrate_paper_trading()

    assert bridge["paper_only"] is True
    assert bridge["paper_bridge"]["intents_built"] >= 0


def test_run_wallet_intelligence_cycle_end_to_end(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    signal_db = tmp_path / "signals.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()

    save_wallet_report(_report(), db_path=str(traders_db))

    def _fake_export(wallet, limit=None, source=None, db_path="data/wallet_activity.db", store=True):
        from src.analysis.wallet_activity import WalletActivityExport, WalletActivityEvent

        event = WalletActivityEvent(
            wallet=wallet,
            timestamp=time.time(),
            event_type="buy",
            market_id="market-1",
            condition_id="condition-1",
            market_slug="btc-up",
            market_title="BTC Up",
            asset="BTC",
            side="up",
            action="buy",
            shares=20.0,
            price=0.55,
            amount=200.0,
            tx_hash="0xbuy",
            raw={},
        )
        export = WalletActivityExport(
            wallet=wallet,
            export_timestamp="2026-06-15T00:00:00Z",
            source="test",
            event_count=1,
            events=[event],
        )
        path = export_dir / f"{wallet}_activity.json"
        path.write_text(json.dumps(_activity_export([_event("buy", amount=200.0)], wallet=wallet)), encoding="utf-8")
        return export

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=export_dir,
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
        activity_exporter=_fake_export,
    )
    classifier = StrategyClassifier(traders_db_path=traders_db)
    engine = SignalEngine(wallet_tracker=tracker, strategy_classifier=classifier, signal_db_path=signal_db)

    result = run_wallet_intelligence_cycle(
        wallet_tracker=tracker,
        strategy_classifier=classifier,
        signal_engine=engine,
        watchlist_limit=5,
    )

    assert result["read_only"] is True
    assert result["paper_only"] is True
    assert result["watchlist"]["watchlist_count"] >= 1
    assert result["processing"]["wallets_processed"] >= 1
    assert len(result["processing"]["strategy_profiles"]) >= 1
