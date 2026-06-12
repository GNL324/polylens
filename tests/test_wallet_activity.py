from __future__ import annotations

import json
import sys
from urllib.error import HTTPError

from src.analysis.wallet_activity import (
    PolymarketActivitySource,
    export_wallet_activity,
    normalize_action,
    normalize_activity_payload,
    normalize_side,
)
from src.analysis.wallet_forensics import compute_basic_metrics, parse_activity_payload
from src.cli import export_wallet_activity_cli
from src.sqlite_utils import closing_connection

WALLET = "0x" + "a" * 40


def _raw(**overrides):
    row = {
        "type": "TRADE",
        "side": "BUY",
        "outcome": "Up",
        "conditionId": "0xcond",
        "slug": "bitcoin-up-or-down",
        "title": "Bitcoin Up or Down",
        "size": "10",
        "price": "0.42",
        "usdcSize": "4.2",
        "timestamp": 100,
        "transactionHash": "0xtx",
    }
    row.update(overrides)
    return row


def test_normalization_variants_and_outcome_indexes():
    assert normalize_action({"action": "BUY"}) == "buy"
    assert normalize_action({"action": "Buy"}) == "buy"
    assert normalize_action({"action": "trade_buy"}) == "buy"
    assert normalize_action({"type": "TRADE", "side": "SELL"}) == "sell"
    assert normalize_side("Yes") == "yes"
    assert normalize_side("Down") == "down"
    assert normalize_side(None, {"outcomeIndex": 1, "title": "Bitcoin Up or Down"}) == "down"
    assert normalize_side(None, {"outcomeIndex": 0, "title": "Election winner"}) == "yes"


def test_parse_malformed_and_preserve_raw_payload():
    events = normalize_activity_payload(
        [
            _raw(action="trade_buy", side="", outcome="Yes"),
            _raw(type="MAKER_REBATE"),
            "bad row",
            _raw(type="REDEEM", side="", outcome="", transactionHash="0xredeem"),
            _raw(type="MERGE", side="", outcome="", transactionHash="0xmerge"),
        ],
        WALLET,
    )
    by_action = {event.action: event for event in events}
    assert set(by_action) == {"buy", "redeem", "merge"}
    assert by_action["buy"].side == "yes"
    assert by_action["buy"].raw["title"] == "Bitcoin Up or Down"
    assert by_action["buy"].asset == "BTC"


def test_duplicate_events_are_deduped_deterministically():
    duplicate = _raw()
    events = normalize_activity_payload([duplicate, dict(duplicate), _raw(transactionHash="0xtx2", timestamp=90)], WALLET)
    assert len(events) == 2
    assert [event.tx_hash for event in events] == ["0xtx2", "0xtx"]


class FakeSource:
    name = "fake"

    def fetch_activity(self, wallet, limit=None):
        rows = [_raw(transactionHash=f"0x{i}", timestamp=i) for i in range(5)]
        return rows[:limit]


def test_export_persists_to_sqlite(tmp_path):
    db_path = tmp_path / "wallet_activity.db"
    export = export_wallet_activity(WALLET, limit=3, source=FakeSource(), db_path=db_path)
    assert export.event_count == 3
    with closing_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wallet_exports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM wallet_events").fetchone()[0] == 3


def test_pagination_and_retry_logic(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "rate limited", {"Retry-After": "0"}, None)
        if "offset=0" in request.full_url:
            return Response([_raw(transactionHash="0x1"), _raw(transactionHash="0x2")])
        return Response([_raw(transactionHash="0x3")])

    monkeypatch.setattr("src.analysis.wallet_activity.urlopen", fake_urlopen)
    source = PolymarketActivitySource(page_size=2, retries=1, retry_backoff_seconds=0, sleep=lambda _: None)
    rows = source.fetch_activity(WALLET, limit=5)
    assert len(rows) == 3
    assert len(calls) == 3
    assert "offset=2" in calls[-1]


def test_graceful_export_failure_returns_empty_export(tmp_path):
    class BrokenSource:
        name = "broken"

        def fetch_activity(self, wallet, limit=None):
            raise RuntimeError("network down")

    export = export_wallet_activity(WALLET, source=BrokenSource(), db_path=tmp_path / "wallet_activity.db")
    assert export.event_count == 0
    assert export.events == []


def test_export_wallet_activity_cli_json_output(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "wallet_activity.db"
    output_path = tmp_path / "wallet.json"

    def fake_build(wallet, limit=None, db_path=None, store=True):
        return export_wallet_activity(wallet, limit=1, source=FakeSource(), db_path=db_path, store=store)

    monkeypatch.setattr("src.cli.build_wallet_activity_export", fake_build)
    payload = export_wallet_activity_cli(WALLET, output=str(output_path), limit=1, as_json=True, db_path=str(db_path))
    printed = json.loads(capsys.readouterr().out)
    assert payload["event_count"] == 1
    assert printed["events"][0]["action"] == "buy"
    assert output_path.exists()


def test_cli_main_dispatch_export_wallet_activity(tmp_path, capsys, monkeypatch):
    from src.cli import main

    db_path = tmp_path / "wallet_activity.db"

    def fake_build(wallet, limit=None, db_path=None, store=True):
        return export_wallet_activity(wallet, limit=1, source=FakeSource(), db_path=db_path, store=store)

    monkeypatch.setattr("src.cli.build_wallet_activity_export", fake_build)
    sys.argv = ["polylens", "export-wallet-activity", "--wallet", WALLET, "--limit", "1", "--db-path", str(db_path), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["wallet"] == WALLET
    assert output["event_count"] == 1


def test_forensics_reads_real_exported_polymarket_activity_shape():
    payload = {
        "wallet": WALLET,
        "event_count": 4,
        "events": [
            {
                "wallet": WALLET,
                "timestamp": 1781292300,
                "event_type": "trade",
                "market_id": "0xbtc",
                "condition_id": "0xbtc",
                "market_slug": "btc-updown-5m-1781292300",
                "market_title": "Bitcoin Up or Down - June 12, 3:25PM-3:30PM ET",
                "asset": "BTC",
                "side": "up",
                "action": "buy",
                "shares": 4.0,
                "price": 0.5,
                "amount": 2.0,
                "tx_hash": "0xbtc-up",
                "raw": {"type": "TRADE", "side": "BUY"},
            },
            {
                "wallet": WALLET,
                "timestamp": 1781292301,
                "event_type": "trade",
                "market_id": "0xbtc",
                "condition_id": "0xbtc",
                "market_slug": "btc-updown-5m-1781292300",
                "market_title": "Bitcoin Up or Down - June 12, 3:25PM-3:30PM ET",
                "asset": "BTC",
                "side": "down",
                "action": "buy",
                "shares": 5.0,
                "price": 0.4,
                "amount": 2.0,
                "tx_hash": "0xbtc-down",
                "raw": {"type": "TRADE", "side": "BUY"},
            },
            {
                "wallet": WALLET,
                "timestamp": 1781292400,
                "event_type": "trade",
                "market_id": "0xeth",
                "condition_id": "0xeth",
                "market_slug": "eth-updown-5m-1781292400",
                "market_title": "Ethereum Up or Down - June 12, 3:30PM-3:35PM ET",
                "asset": "ETH",
                "side": "up",
                "action": "sell",
                "shares": 6.0,
                "price": 0.6,
                "amount": 3.6,
                "tx_hash": "0xeth-up",
                "raw": {"type": "TRADE", "side": "SELL"},
            },
            {
                "wallet": WALLET,
                "timestamp": 1781292500,
                "event_type": "trade",
                "market_id": "0xsol",
                "condition_id": "0xsol",
                "market_slug": "sol-updown-5m-1781292500",
                "market_title": "Solana Up or Down - June 12, 3:35PM-3:40PM ET",
                "asset": "SOL",
                "side": "yes",
                "action": "buy",
                "shares": 7.0,
                "price": 0.7,
                "amount": 4.9,
                "tx_hash": "0xsol-yes",
                "raw": {"type": "TRADE", "side": "BUY"},
            },
        ],
    }

    activities = parse_activity_payload(payload, WALLET)
    metrics = compute_basic_metrics(activities)

    assert {activity.market_id for activity in activities} == {"0xbtc", "0xeth", "0xsol"}
    assert {activity.asset for activity in activities} == {"BTC", "ETH", "SOL"}
    assert metrics["markets_traded"] == 3
    assert metrics["overlap_markets"] == ["Bitcoin Up or Down - June 12, 3:25PM-3:30PM ET"]
    assert metrics["btc_volume"] == 4.0
    assert metrics["eth_volume"] == 3.6
    assert metrics["sol_volume"] == 4.9
    assert metrics["other_volume"] == 0.0
