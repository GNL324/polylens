from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src.analysis.trader_replay import (
    TraderReplayReport,
    build_trader_replay,
    infer_market_type,
    load_wallet_replay_events,
)
from src.analysis.wallet_activity import WalletActivityEvent

WALLET = "0x" + "a" * 40


def _raw_event(
    *,
    timestamp: float,
    action: str = "buy",
    side: str = "up",
    title: str = "Bitcoin Up or Down - 5 minute",
    slug: str = "bitcoin-up-or-down-5m",
    amount: float = 10.0,
    tx_hash: str | None = None,
) -> dict:
    return {
        "wallet": WALLET,
        "timestamp": timestamp,
        "event_type": action,
        "market_id": f"market-{int(timestamp)}",
        "condition_id": f"cond-{int(timestamp)}",
        "market_slug": slug,
        "market_title": title,
        "asset": "BTC" if "Bitcoin" in title else "ETH",
        "side": side,
        "action": action,
        "shares": 20.0,
        "price": 0.5,
        "amount": amount,
        "tx_hash": tx_hash or f"0xtx-{int(timestamp)}-{action}",
        "raw": {},
    }


def _write_export(tmp_path: Path, events: list[dict]) -> Path:
    export_dir = tmp_path / "wallets"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_path = export_dir / f"{WALLET}_activity.json"
    export_path.write_text(
        json.dumps(
            {
                "wallet": WALLET,
                "export_timestamp": "2026-06-12T00:00:00Z",
                "source": "test",
                "event_count": len(events),
                "events": events,
            }
        ),
        encoding="utf-8",
    )
    return export_dir


def _fixture_events() -> list[dict]:
    base = 1_718_000_000.0
    return [
        _raw_event(timestamp=base, action="buy", side="up", amount=12.0, tx_hash="0x1"),
        _raw_event(timestamp=base + 300, action="buy", side="down", amount=8.0, tx_hash="0x2"),
        _raw_event(timestamp=base + 600, action="merge", side="", amount=0.0, tx_hash="0x3"),
        _raw_event(timestamp=base + 900, action="redeem", side="", amount=15.0, tx_hash="0x4"),
        _raw_event(
            timestamp=base + 5_400,
            action="buy",
            side="up",
            title="Ethereum Up or Down - 15 minute",
            slug="ethereum-up-or-down-15m",
            amount=25.0,
            tx_hash="0x5",
        ),
        _raw_event(
            timestamp=base + 86_400,
            action="sell",
            side="down",
            title="Bitcoin Up or Down - hourly",
            slug="bitcoin-up-or-down-hourly",
            amount=40.0,
            tx_hash="0x6",
        ),
    ]


def test_infer_market_type_from_titles():
    assert infer_market_type("Bitcoin Up or Down - 5 minute", "btc-5m", "BTC") == "BTC 5m"
    assert infer_market_type("Ethereum Up or Down - 15 minute", "eth-15m", "ETH") == "ETH 15m"
    assert infer_market_type("Bitcoin Up or Down - hourly", "btc-hourly", "BTC") == "BTC hourly"


def test_build_trader_replay_orders_timeline_and_stats(tmp_path):
    export_dir = _write_export(tmp_path, _fixture_events())

    report = build_trader_replay(WALLET, wallet_export_dir=export_dir, export_if_missing=False)

    assert isinstance(report, TraderReplayReport)
    assert report.event_count == 6
    assert report.timeline[0]["action"] == "buy"
    assert report.timeline[-1]["action"] == "sell"
    assert report.statistics["buy_count"] == 3
    assert report.statistics["sell_count"] == 1
    assert report.statistics["merge_count"] == 1
    assert report.statistics["redeem_count"] == 1
    assert report.statistics["markets_traded"] == 6
    assert report.statistics["btc_volume"] > 0
    assert report.statistics["eth_volume"] > 0
    assert report.statistics["active_days"] == 2
    assert report.statistics["largest_trade"] == 40.0


def test_build_trader_replay_patterns_and_sessions(tmp_path):
    export_dir = _write_export(tmp_path, _fixture_events())

    report = build_trader_replay(WALLET, wallet_export_dir=export_dir, export_if_missing=False)

    assert "BTC" in report.patterns["preferred_assets"]
    assert "buy" in report.patterns["preferred_actions"]
    assert report.patterns["most_active_hour_utc"] is not None
    assert report.patterns["average_events_per_day"] > 0
    assert report.patterns["merge_to_trade_ratio"] > 0
    assert report.patterns["redeem_to_trade_ratio"] > 0
    assert "BTC 5m" in report.patterns["top_market_types"]
    assert report.patterns["session_count"] == 3
    assert report.patterns["largest_session_event_count"] == 4


def test_recent_timeline_limit(tmp_path):
    export_dir = _write_export(tmp_path, _fixture_events())

    report = build_trader_replay(
        WALLET,
        wallet_export_dir=export_dir,
        export_if_missing=False,
        recent_limit=2,
    )

    assert len(report.timeline) == 2
    assert report.event_count == 6


def test_summary_mode_omits_timeline(tmp_path):
    export_dir = _write_export(tmp_path, _fixture_events())

    report = build_trader_replay(
        WALLET,
        wallet_export_dir=export_dir,
        export_if_missing=False,
        include_timeline=False,
    )

    payload = report.to_dict(include_timeline=False)
    assert "timeline" not in payload
    assert payload["statistics"]["activity_count"] == 6


def test_load_wallet_replay_events_from_export(tmp_path):
    export_dir = _write_export(tmp_path, _fixture_events())

    events = load_wallet_replay_events(WALLET, wallet_export_dir=export_dir, export_if_missing=False)

    assert len(events) == 6
    assert all(isinstance(event, WalletActivityEvent) for event in events)
    assert events[0].timestamp <= events[-1].timestamp


def test_build_trader_replay_exports_when_missing(tmp_path, monkeypatch):
    export_dir = tmp_path / "wallets"
    export_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_export(wallet: str, limit: int | None = None, **kwargs):
        events = normalize_fixture_events(_fixture_events())
        captured["wallet"] = wallet
        captured["limit"] = limit
        from src.analysis.wallet_activity import WalletActivityExport

        return WalletActivityExport(
            wallet=wallet,
            export_timestamp="2026-06-12T00:00:00Z",
            source="fake",
            event_count=len(events),
            events=events,
        )

    monkeypatch.setattr("src.analysis.trader_replay.export_wallet_activity", fake_export)

    report = build_trader_replay(WALLET, wallet_export_dir=export_dir)

    assert captured["wallet"] == WALLET
    assert (export_dir / f"{WALLET}_activity.json").exists()
    assert report.event_count == 6


def test_trader_replay_cli_json_output(tmp_path, capsys, monkeypatch):
    export_dir = _write_export(tmp_path, _fixture_events())
    monkeypatch.setattr(
        "src.cli.build_trader_replay",
        lambda wallet, **kwargs: build_trader_replay(wallet, wallet_export_dir=export_dir, export_if_missing=False),
    )
    monkeypatch.setattr(sys, "argv", ["polylens", "trader-replay", "--wallet", WALLET, "--wallet-export-dir", str(export_dir)])

    from src.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["wallet"] == WALLET
    assert payload["event_count"] == 6
    assert payload["timeline"]


def test_trader_replay_cli_summary_mode(tmp_path, capsys, monkeypatch):
    export_dir = _write_export(tmp_path, _fixture_events())
    monkeypatch.setattr(
        "src.cli.build_trader_replay",
        lambda wallet, **kwargs: build_trader_replay(wallet, wallet_export_dir=export_dir, export_if_missing=False),
    )
    monkeypatch.setattr(sys, "argv", ["polylens", "trader-replay", "--wallet", WALLET, "--summary", "--json"])

    from src.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert "timeline" not in payload
    assert payload["patterns"]["session_count"] == 3


def test_trader_replay_cli_recent_limit(tmp_path, capsys, monkeypatch):
    export_dir = _write_export(tmp_path, _fixture_events())
    monkeypatch.setattr(
        "src.cli.build_trader_replay",
        lambda wallet, **kwargs: build_trader_replay(
            wallet,
            wallet_export_dir=export_dir,
            export_if_missing=False,
            recent_limit=kwargs.get("recent_limit"),
        ),
    )
    monkeypatch.setattr(sys, "argv", ["polylens", "trader-replay", "--wallet", WALLET, "--recent", "3", "--json"])

    from src.cli import main

    main()
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["timeline"]) == 3


def test_large_timeline_performance(tmp_path):
    export_dir = tmp_path / "wallets"
    export_dir.mkdir(parents=True, exist_ok=True)
    base = 1_718_000_000.0
    events = [
        _raw_event(timestamp=base + index * 30, action="buy" if index % 2 == 0 else "sell", tx_hash=f"0x{index}")
        for index in range(5_500)
    ]
    _write_export(tmp_path, events)

    report = build_trader_replay(WALLET, wallet_export_dir=export_dir, export_if_missing=False, recent_limit=100)

    assert report.event_count == 5_500
    assert len(report.timeline) == 100
    assert report.statistics["activity_count"] == 5_500


def normalize_fixture_events(rows: list[dict]):
    from src.analysis.wallet_activity import normalize_activity_payload

    return normalize_activity_payload(rows, WALLET)


def test_missing_export_raises_when_export_disabled(tmp_path):
    export_dir = tmp_path / "wallets"
    export_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(FileNotFoundError):
        load_wallet_replay_events(WALLET, wallet_export_dir=export_dir, export_if_missing=False)
