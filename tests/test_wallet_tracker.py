from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate
from src.analysis.trader_registry import save_wallet_report
from src.analysis.wallet_activity import WalletActivityEvent, WalletActivityExport
from src.intelligence.wallet_tracker import WalletTracker, init_wallet_watchlist_db

WALLET_A = "0x" + "a1" * 20
WALLET_B = "0x" + "b2" * 20


def _report(wallet: str, classification: str = "arbitrage_trader", watch_score: int = 85):
    return {
        "wallet": wallet,
        "classification": classification,
        "confidence": 0.9,
        "watch_score": watch_score,
        "metrics": {
            "trade_count": 80,
            "markets_traded": 30,
            "overlap_count": 12,
            "overlap_ratio": 0.35,
            "merge_count": 8,
            "redeem_count": 4,
            "buy_volume": 2500,
            "sell_volume": 800,
            "btc_volume": 2000,
            "eth_volume": 500,
            "sol_volume": 0,
            "arbitrage_opportunities": 5,
            "arbitrage_volume": 400,
            "average_sum_price": 0.96,
        },
        "signals": [{"name": "high_overlap_frequency"}],
    }


@dataclass
class FakeExport:
    wallet: str
    export_timestamp: str = "2026-06-15T00:00:00Z"
    source: str = "test"
    event_count: int = 1
    events: list[WalletActivityEvent] | None = None

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = [
                WalletActivityEvent(
                    wallet=self.wallet,
                    timestamp=1_700_000_000.0,
                    event_type="buy",
                    market_id="market-1",
                    condition_id="condition-1",
                    market_slug="btc-up",
                    market_title="BTC Up",
                    asset="BTC",
                    side="up",
                    action="buy",
                    shares=10.0,
                    price=0.5,
                    amount=5.0,
                    tx_hash="0xabc",
                    raw={},
                )
            ]
        self.event_count = len(self.events)


def _fake_exporter_factory(exports: dict[str, FakeExport]):
    def _export(
        wallet: str,
        limit: int | None = None,
        source=None,
        db_path="data/wallet_activity.db",
        store: bool = True,
    ) -> WalletActivityExport:
        export = exports[wallet]
        return WalletActivityExport(
            wallet=export.wallet,
            export_timestamp=export.export_timestamp,
            source=export.source,
            event_count=export.event_count,
            events=export.events or [],
        )

    return _export


def test_discover_top_wallets_merges_sources(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps([WALLET_A]), encoding="utf-8")

    save_wallet_report(_report(WALLET_B, watch_score=70), db_path=str(traders_db))
    save_discovery_candidate(
        TraderDiscoveryCandidate(wallet=WALLET_B, source="test", discovery_score=60, evidence_count=3),
        db_path=str(discovery_db),
    )

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=watchlist,
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    wallets = tracker.discover_top_wallets(limit=10)
    assert WALLET_A in wallets
    assert WALLET_B in wallets


def test_build_ranked_watchlist_orders_by_composite_score(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(WALLET_A, watch_score=90), db_path=str(traders_db))
    save_wallet_report(_report(WALLET_B, classification="mixed", watch_score=40), db_path=str(traders_db))

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    entries = tracker.build_ranked_watchlist(limit=10)
    assert entries[0].wallet == WALLET_A
    assert entries[0].rank == 1
    assert entries[0].composite_score >= entries[1].composite_score


def test_persist_and_load_watchlist(tmp_path):
    traders_db = tmp_path / "traders.db"
    save_wallet_report(_report(WALLET_A), db_path=str(traders_db))

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
    )
    result = tracker.persist_watchlist()
    assert result["watchlist_count"] >= 1
    init_wallet_watchlist_db(traders_db)
    loaded = tracker.load_watchlist()
    assert loaded[0].wallet == WALLET_A


def test_refresh_wallet_writes_export_file(tmp_path):
    traders_db = tmp_path / "traders.db"
    export_dir = tmp_path / "wallets"
    export_dir.mkdir()
    fake = _fake_exporter_factory({WALLET_A: FakeExport(wallet=WALLET_A)})

    tracker = WalletTracker(
        traders_db_path=traders_db,
        discovery_db_path=tmp_path / "discovery.db",
        wallet_export_dir=export_dir,
        watchlist_path=tmp_path / "watchlist.json",
        paper_copy_db_path=tmp_path / "paper_copy.db",
        activity_exporter=fake,
    )
    tracker.refresh_wallet_history(WALLET_A, incremental=False)
    export_path = export_dir / f"{WALLET_A}_activity.json"
    assert export_path.exists()
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["wallet"] == WALLET_A
    assert payload["event_count"] == 1
