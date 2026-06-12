from __future__ import annotations

import json
import sys
from io import BytesIO
from urllib.error import HTTPError

from src.analysis.trader_registry import load_wallet_report, save_wallet_report
from src.analysis.trader_scanner import (
    discover_wallets,
    scan_wallet,
    scan_wallets,
    top_arbitrage_traders,
    top_directional_traders,
    top_market_makers,
)
from src.analysis.wallet_activity import PolymarketActivitySource

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
WALLET_C = "0x" + "c" * 40


class FakeActivitySource:
    name = "fake-activity"

    def fetch_activity(self, wallet, limit=None):
        rows = [
            _activity("TRADE", "BUY", "Up", "0xbtc", "Bitcoin Up or Down - test", "btc-updown", 10, 0.4, 4.0, "0x1"),
            _activity("TRADE", "BUY", "Down", "0xbtc", "Bitcoin Up or Down - test", "btc-updown", 10, 0.5, 5.0, "0x2"),
            _activity("MERGE", "", "", "0xbtc", "Bitcoin Up or Down - test", "btc-updown", 4, 0.0, 4.0, "0x3"),
            _activity("MERGE", "", "", "0xeth", "Ethereum Up or Down - test", "eth-updown", 4, 0.0, 4.0, "0x4"),
            _activity("MERGE", "", "", "0xsol", "Solana Up or Down - test", "sol-updown", 4, 0.0, 4.0, "0x5"),
            _activity("REDEEM", "", "", "0xbtc", "Bitcoin Up or Down - test", "btc-updown", 2, 0.0, 2.0, "0x6"),
            _activity("REDEEM", "", "", "0xeth", "Ethereum Up or Down - test", "eth-updown", 2, 0.0, 2.0, "0x7"),
        ]
        return rows[:limit] if limit else rows


def _activity(activity_type, side, outcome, condition_id, title, slug, size, price, usdc_size, tx_hash):
    return {
        "type": activity_type,
        "side": side,
        "outcome": outcome,
        "conditionId": condition_id,
        "slug": slug,
        "title": title,
        "size": size,
        "price": price,
        "usdcSize": usdc_size,
        "timestamp": 100 + int(tx_hash[-1]),
        "transactionHash": tx_hash,
    }


def _report(wallet, classification, confidence, watch_score=50):
    return {
        "schema_version": 1,
        "wallet": wallet,
        "classification": classification,
        "confidence": confidence,
        "watch_score": watch_score,
        "signals": [],
        "metrics": {"trade_count": 10, "buy_volume": 100, "markets_traded": 3},
    }


def test_discover_wallets_from_watchlist_and_registry(tmp_path):
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps([WALLET_A, WALLET_B, WALLET_A]), encoding="utf-8")
    db_path = tmp_path / "traders.db"
    save_wallet_report(_report(WALLET_C, "market_maker", 0.8), db_path=str(db_path))

    wallets = discover_wallets(watchlist=watchlist, registry_db_path=db_path, include_registry=True)

    assert wallets == [WALLET_A, WALLET_B, WALLET_C]


def test_scan_wallet_updates_registry_and_stores_history(tmp_path):
    db_path = tmp_path / "traders.db"
    activity_db = tmp_path / "wallet_activity.db"
    output_dir = tmp_path / "wallets"

    result = scan_wallet(
        WALLET_A,
        activity_source=FakeActivitySource(),
        wallet_activity_db_path=activity_db,
        traders_db_path=db_path,
        output_dir=output_dir,
    )

    assert result["accepted"] is True
    assert result["wallet"] == WALLET_A
    assert result["classification"] == "market_maker"
    assert result["watch_score"] > 0
    assert (output_dir / f"{WALLET_A}_activity.json").exists()
    loaded = load_wallet_report(WALLET_A, db_path=str(db_path))
    assert loaded is not None
    assert loaded.classification == "market_maker"


def test_scan_wallets_summary_counts(tmp_path):
    result = scan_wallets(
        wallets=[WALLET_A, WALLET_B],
        activity_source=FakeActivitySource(),
        wallet_activity_db_path=tmp_path / "wallet_activity.db",
        traders_db_path=tmp_path / "traders.db",
        output_dir=tmp_path / "wallets",
    )

    assert result["wallets_scanned"] == 2
    assert result["market_makers"] == 2
    assert result["arbitrage_traders"] == 0
    assert result["scan_failures"] == 0


def test_scan_wallets_succeeds_with_partial_activity_export(tmp_path, monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                [
                    _activity("TRADE", "BUY", "Up", "0xbtc", "Bitcoin Up or Down - test", "btc-updown", 10, 0.4, 4.0, "0x1"),
                    _activity("TRADE", "BUY", "Down", "0xbtc", "Bitcoin Up or Down - test", "btc-updown", 10, 0.5, 5.0, "0x2"),
                ]
            ).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "offset=0" in request.full_url:
            return Response()
        body = BytesIO(b'{"error":"max historical activity offset of 3000 exceeded"}')
        raise HTTPError(request.full_url, 400, "bad request", {}, body)

    monkeypatch.setattr("src.analysis.wallet_activity.urlopen", fake_urlopen)
    source = PolymarketActivitySource(page_size=2, max_historical_offset=10, retries=0)

    result = scan_wallets(
        wallets=[WALLET_A],
        activity_source=source,
        wallet_activity_db_path=tmp_path / "wallet_activity.db",
        traders_db_path=tmp_path / "traders.db",
        output_dir=tmp_path / "wallets",
    )

    assert result["wallets_scanned"] == 1
    assert result["scan_failures"] == 0
    assert result["results"][0]["accepted"] is True
    assert result["results"][0]["event_count"] == 2
    assert result["results"][0]["classification"]
    assert len(calls) == 2


def test_leaderboard_helpers_use_registry(tmp_path):
    db_path = tmp_path / "traders.db"
    save_wallet_report(_report(WALLET_A, "market_maker", 0.9), watch_score=90, db_path=str(db_path))
    save_wallet_report(_report(WALLET_B, "arbitrage_trader", 0.8), watch_score=80, db_path=str(db_path))
    save_wallet_report(_report(WALLET_C, "quantitative_directional", 0.7), watch_score=70, db_path=str(db_path))

    assert top_market_makers(db_path=db_path)[0].wallet == WALLET_A
    assert top_arbitrage_traders(db_path=db_path)[0].wallet == WALLET_B
    assert top_directional_traders(db_path=db_path)[0].wallet == WALLET_C


def test_scan_top_traders_cli_json_output(capsys, monkeypatch):
    from src.cli import main

    expected = {
        "wallets_scanned": 1,
        "market_makers": 1,
        "arbitrage_traders": 0,
        "quantitative_directional": 0,
        "mixed": 0,
        "unknown": 0,
        "scan_failures": 0,
        "results": [{"accepted": True, "wallet": WALLET_A, "classification": "market_maker"}],
    }

    def fake_scan(**kwargs):
        assert kwargs["wallets"] == [WALLET_A]
        assert kwargs["include_registry"] is False
        return expected

    monkeypatch.setattr("src.cli.scan_trader_wallets", fake_scan)
    sys.argv = ["polylens", "scan-top-traders", "--wallet", WALLET_A, "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output == expected
