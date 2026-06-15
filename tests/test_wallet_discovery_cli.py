from __future__ import annotations

import json
from io import StringIO
import sys

from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_discovery import WalletDiscoveryEngine, init_wallet_discovery_db

WALLET = "0x" + "a" * 40


def _report():
    return {
        "wallet": WALLET,
        "classification": "market_maker",
        "confidence": 0.85,
        "watch_score": 75,
        "metrics": {"trade_count": 50, "markets_traded": 20, "overlap_ratio": 0.3, "merge_count": 5, "redeem_count": 2, "buy_volume": 1000, "sell_volume": 400, "btc_volume": 800, "eth_volume": 200, "sol_volume": 0},
        "signals": [],
    }


def test_wallet_discover_cli_human_output(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text("[]", encoding="utf-8")
    save_wallet_report(_report(), db_path=str(traders_db))

    from src.cli import wallet_discover_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    result = wallet_discover_cli(as_json=False, limit=5)
    assert result["candidates_found"] >= 0
    assert "Discovered" in captured.getvalue()


def test_wallet_rank_cli_json(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_discovery_db(traders_db, discovery_db)
    save_wallet_report(_report(), db_path=str(traders_db))

    monkeypatch.chdir(tmp_path)
    import src.intelligence.wallet_discovery as discovery_module
    monkeypatch.setattr(discovery_module, "DEFAULT_TRADERS_DB", str(traders_db))
    monkeypatch.setattr(discovery_module, "DEFAULT_TRADER_DISCOVERY_DB", str(discovery_db))

    from src.cli import wallet_rank_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    result = wallet_rank_cli(as_json=True, limit=5)
    assert "ranked" in result
    payload = json.loads(captured.getvalue())
    assert payload["read_only"] is True


def test_wallet_watchlist_cli(tmp_path, monkeypatch):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    engine = WalletDiscoveryEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        wallet_export_dir=tmp_path / "wallets",
        watchlist_path=tmp_path / "watchlist.json",
    )
    from src.intelligence.wallet_scoring import WalletScorer

    scorer = WalletScorer(traders_db_path=traders_db, discovery_db_path=discovery_db)
    save_wallet_report(_report(), db_path=str(traders_db))
    row = scorer.score_wallet(WALLET)
    row = type(row)(wallet=row.wallet, score=72.0, confidence=0.7, rank=1, category=row.category, components=row.components, metrics=row.metrics)
    engine.apply_lifecycle([row])

    from src.cli import wallet_watchlist_cli

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    result = wallet_watchlist_cli(as_json=False, state="active", limit=5)
    assert "wallets" in result
