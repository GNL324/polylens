from __future__ import annotations

import json
import sys

from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    TraderDiscoveryCandidate,
    discover_from_activity_export,
    discover_from_wallet,
    discover_market_participants,
    discover_from_registry,
    discovery_report,
    load_discovered_wallets,
    save_discovery_candidate,
    score_counterparty_candidates,
)
from src.analysis.trader_registry import save_wallet_report

SOURCE = "0x" + "1" * 40
WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_C = "0x" + "c" * 40


def _event(wallet: str, market: str, title: str = "Bitcoin Up or Down"):
    return {
        "market_id": market,
        "condition_id": market,
        "market_slug": f"{market}-slug",
        "market_title": title,
        "raw": {
            "counterpartyWallet": wallet,
            "maker": wallet.upper(),
            "proxyWallet": SOURCE,
        },
    }


def _export_payload():
    return {
        "wallet": SOURCE,
        "events": [
            _event(WALLET_A, "0xmarket1"),
            _event(WALLET_A, "0xmarket1"),
            _event(WALLET_A, "0xmarket2", title="Ethereum Up or Down"),
            _event(WALLET_B, "0xmarket1"),
            _event(SOURCE, "0xmarket1"),
        ],
    }


def _report(wallet: str, classification: str = "market_maker"):
    return {
        "wallet": wallet,
        "classification": classification,
        "confidence": 0.8,
        "metrics": {"trade_count": 10, "buy_volume": 100, "markets_traded": 3},
        "signals": [],
    }


def test_discovery_scoring_from_counterparty_payload():
    candidates = score_counterparty_candidates(_export_payload(), source_wallet=SOURCE)

    assert [candidate.wallet for candidate in candidates] == [WALLET_A, WALLET_B]
    assert candidates[0].evidence_count == 3
    assert candidates[0].markets_seen == ["0xmarket1", "0xmarket2"]
    assert candidates[0].discovery_score > candidates[1].discovery_score


def test_market_participant_discovery_excludes_seed(monkeypatch):
    payload = {
        "wallet": SOURCE,
        "events": [
            {
                "condition_id": "0xmarket1",
                "market_id": "0xmarket1",
                "market_slug": "btc-updown",
                "raw": {"conditionId": "0xmarket1", "proxyWallet": SOURCE},
            }
        ],
    }

    def fake_fetch(ref, limit=100):
        assert ref["conditionId"] == "0xmarket1"
        return [
            {"conditionId": "0xmarket1", "proxyWallet": SOURCE},
            {"conditionId": "0xmarket1", "proxyWallet": WALLET_A},
            {"conditionId": "0xmarket1", "proxyWallet": "0x" + "A" * 40},
            {"conditionId": "0xmarket1", "proxyWallet": WALLET_B},
            {"conditionId": "0xother", "proxyWallet": WALLET_C},
            {"conditionId": "0xmarket1", "proxyWallet": "0xshort"},
        ]

    monkeypatch.setattr("src.analysis.trader_discovery._fetch_market_activity", fake_fetch)
    candidates = discover_market_participants(payload, source_wallet=SOURCE)

    assert [candidate.wallet for candidate in candidates] == [WALLET_A, WALLET_B]
    assert candidates[0].evidence_count == 2
    assert candidates[0].source == "co_market_activity"
    assert SOURCE not in {candidate.wallet for candidate in candidates}


def test_discover_from_wallet_returns_new_co_market_wallets(tmp_path, monkeypatch):
    output_dir = tmp_path / "wallets"
    output_dir.mkdir()
    (output_dir / f"{SOURCE}_activity.json").write_text(json.dumps(_export_payload()), encoding="utf-8")

    def fake_fetch(ref, limit=100):
        return [
            {"conditionId": ref["conditionId"], "proxyWallet": SOURCE},
            {"conditionId": ref["conditionId"], "proxyWallet": WALLET_A},
            {"conditionId": ref["conditionId"], "proxyWallet": WALLET_B},
        ]

    monkeypatch.setattr("src.analysis.trader_discovery._fetch_market_activity", fake_fetch)
    wallets = discover_from_wallet(SOURCE, output_dir=output_dir, db_path=tmp_path / "discovery.db")

    assert wallets == [WALLET_A, WALLET_B]
    assert SOURCE not in wallets


def test_discover_from_activity_export_dedupes_and_persists(tmp_path):
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(_export_payload()), encoding="utf-8")
    db_path = tmp_path / "trader_discovery.db"

    wallets = discover_from_activity_export(path, db_path=db_path)
    saved = load_discovered_wallets(db_path)

    assert wallets == [WALLET_A, WALLET_B]
    assert [candidate.wallet for candidate in saved] == [WALLET_A, WALLET_B]
    assert saved[0].discovery_score >= saved[1].discovery_score


def test_discover_from_registry_expansion(tmp_path):
    db_path = tmp_path / "traders.db"
    save_wallet_report(_report(WALLET_A), watch_score=90, db_path=str(db_path))
    save_wallet_report(_report(WALLET_B, "arbitrage_trader"), watch_score=80, db_path=str(db_path))

    assert discover_from_registry(db_path=db_path) == [WALLET_A, WALLET_B]


def test_discovery_database_updates_existing_candidate(tmp_path):
    db_path = tmp_path / "trader_discovery.db"
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "watchlist", 35, 1), db_path=db_path)
    save_discovery_candidate(TraderDiscoveryCandidate(WALLET_A, "counterparty", 80, 5, ["0xmarket"]), db_path=db_path)

    saved = load_discovered_wallets(db_path)

    assert len(saved) == 1
    assert saved[0].wallet == WALLET_A
    assert saved[0].discovery_score == 80
    assert saved[0].source == "counterparty"
    assert saved[0].evidence_count == 5


def test_discovery_report_with_scan_integration(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(_export_payload()), encoding="utf-8")

    def fake_scan_wallets(**kwargs):
        assert kwargs["wallets"] == [WALLET_A, WALLET_B]
        assert kwargs["include_registry"] is False
        return {"wallets_scanned": 2, "scan_failures": 0, "results": []}

    monkeypatch.setattr("src.analysis.trader_discovery.scan_wallets", fake_scan_wallets)
    result = discovery_report(activity_export=path, discovery_db_path=tmp_path / "discovery.db", scan=True)

    assert result["wallets_discovered"] == 2
    assert result["new_wallets"] == 2
    assert result["scan"]["wallets_scanned"] == 2


def test_discover_traders_cli_json_output(tmp_path, capsys, monkeypatch):
    from src.cli import main

    expected = {
        "wallets_discovered": 1,
        "new_wallets": 1,
        "existing_wallets": 0,
        "top_candidates": [{"wallet": WALLET_A, "score": 94, "source": "counterparty"}],
    }

    def fake_report(**kwargs):
        assert kwargs["wallet"] == WALLET_A
        assert kwargs["scan"] is True
        return expected

    monkeypatch.setattr("src.cli.build_trader_discovery_report", fake_report)
    sys.argv = ["polylens", "discover-traders", "--wallet", WALLET_A, "--scan", "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
