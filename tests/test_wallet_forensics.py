from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src.analysis.wallet_forensics import (
    SCHEMA_VERSION,
    WalletActivity,
    analyze_wallet_forensics,
    build_wallet_forensics_report,
    compute_arbitrage_metrics,
    compute_basic_metrics,
    load_wallet_activities,
    parse_activity_record,
    parse_activity_payload,
)
from src.cli import wallet_forensics_cli

FIXTURES = Path(__file__).parent / "fixtures" / "wallet_forensics"


def _activity(**overrides) -> dict:
    base = {
        "conditionId": "0xtest",
        "outcome": "Up",
        "price": 0.5,
        "side": "BUY",
        "size": 10.0,
        "timestamp": 100,
        "title": "Bitcoin Up or Down - test window",
        "type": "TRADE",
        "usdcSize": 5.0,
    }
    base.update(overrides)
    return base


def test_parse_activity_record_trade_buy_sell_redeem_merge():
    wallet = "0x" + "1" * 40
    buy = parse_activity_record(_activity(type="TRADE", side="BUY", outcome="Up"), wallet)
    sell = parse_activity_record(_activity(type="TRADE", side="SELL", outcome="Down"), wallet)
    redeem = parse_activity_record(_activity(type="REDEEM", side="", outcome=""), wallet)
    merge = parse_activity_record(_activity(type="MERGE", side="", outcome=""), wallet)
    skipped = parse_activity_record(_activity(type="MAKER_REBATE"), wallet)

    assert buy is not None and buy.action == "buy" and buy.side == "up" and buy.price_cents == 50.0
    assert sell is not None and sell.action == "sell" and sell.side == "down"
    assert redeem is not None and redeem.action == "redeem"
    assert merge is not None and merge.action == "merge"
    assert skipped is None
    assert buy.asset == "BTC"


def test_overlap_detection_up_down_and_yes_no():
    wallet = "0x" + "2" * 40
    activities = parse_activity_payload(
        [
            _activity(conditionId="0xbtc-a", title="BTC market A", outcome="Up"),
            _activity(conditionId="0xbtc-a", title="BTC market A", outcome="Down"),
            _activity(conditionId="0xelection", title="Election market", outcome="Yes"),
            _activity(conditionId="0xelection", title="Election market", outcome="No"),
            _activity(conditionId="0xone-sided", title="One-sided market", outcome="Yes"),
        ],
        wallet,
    )
    metrics = compute_basic_metrics(activities)
    assert metrics["overlap_count"] == 2
    assert metrics["overlap_ratio"] == round(2 / 3, 4)
    assert "BTC market A" in metrics["overlap_markets"]
    assert "Election market" in metrics["overlap_markets"]


def test_merge_and_redeem_detection():
    wallet = "0x" + "3" * 40
    activities = parse_activity_payload(
        [
            _activity(type="MERGE", outcome="", side=""),
            _activity(type="MERGE", outcome="", side=""),
            _activity(type="REDEEM", outcome="", side="", usdcSize=25.0, size=25.0),
            _activity(type="TRADE", side="BUY"),
        ],
        wallet,
    )
    metrics = compute_basic_metrics(activities)
    assert metrics["merge_count"] == 2
    assert metrics["redeem_count"] == 1
    assert metrics["buy_count"] == 1


def test_arbitrage_detection_sum_price_buckets():
    wallet = "0x" + "4" * 40
    activities = [
        WalletActivity(wallet, "Arb A", "OTHER", "yes", "buy", 42.0, 100, 42.0, 1),
        WalletActivity(wallet, "Arb A", "OTHER", "no", "buy", 52.0, 100, 52.0, 2),
        WalletActivity(wallet, "Neutral B", "OTHER", "yes", "buy", 50.0, 100, 50.0, 3),
        WalletActivity(wallet, "Neutral B", "OTHER", "no", "buy", 50.0, 100, 50.0, 4),
        WalletActivity(wallet, "Overpay C", "OTHER", "yes", "buy", 55.0, 100, 55.0, 5),
        WalletActivity(wallet, "Overpay C", "OTHER", "no", "buy", 52.0, 100, 52.0, 6),
    ]
    metrics = compute_arbitrage_metrics(activities)
    assert metrics["arbitrage_opportunities"] == 1
    assert metrics["neutral_sum_count"] == 1
    assert metrics["overpay_count"] == 1
    assert metrics["minimum_sum_price"] == 94.0
    assert metrics["maximum_sum_price"] == 107.0
    assert metrics["average_sum_price"] == 100.3333


def test_classification_market_maker_fixture():
    report = build_wallet_forensics_report(FIXTURES / "market_maker_wallet.json")
    assert report.classification == "market_maker"
    assert report.confidence >= 0.7
    assert report.metrics["merge_count"] >= 2
    assert report.metrics["redeem_count"] >= 2
    assert report.metrics["overlap_count"] >= 2
    signal_names = {signal["name"] for signal in report.signals}
    assert "high_merge_frequency" in signal_names
    assert "redeem_heavy_behavior" in signal_names


def test_classification_arbitrage_fixture():
    report = build_wallet_forensics_report(FIXTURES / "arbitrage_wallet.json")
    assert report.classification == "arbitrage_trader"
    assert report.metrics["arbitrage_opportunities"] >= 2
    assert report.metrics["minimum_sum_price"] < 100


def test_classification_directional_fixture():
    report = build_wallet_forensics_report(FIXTURES / "directional_wallet.json")
    assert report.classification == "quantitative_directional"
    assert report.metrics["overlap_count"] == 0
    assert report.metrics["merge_count"] == 0


def test_load_wallet_activities_allows_empty_events(tmp_path):
    payload_path = tmp_path / "empty_events.json"
    payload_path.write_text(
        json.dumps({"wallet": "0x" + "a" * 40, "events": []}),
        encoding="utf-8",
    )
    activities, wallet = load_wallet_activities(payload_path)
    assert wallet == "0x" + "a" * 40
    assert activities == []


def test_build_wallet_forensics_report_with_empty_events_is_unknown(tmp_path):
    payload_path = tmp_path / "empty_events.json"
    payload_path.write_text(
        json.dumps({"wallet": "0x" + "b" * 40, "events": []}),
        encoding="utf-8",
    )
    report = build_wallet_forensics_report(payload_path, wallet="0x" + "b" * 40)
    assert report.classification == "unknown"
    assert report.confidence == 0.0


def test_load_wallet_activities_requires_wallet_when_missing(tmp_path):
    payload_path = FIXTURES / "directional_wallet.json"
    activities, wallet = load_wallet_activities(payload_path)
    assert wallet.startswith("0x")
    assert len(activities) == 6
    bare_path = tmp_path / "no_wallet.json"
    bare_path.write_text(json.dumps([{"type": "TRADE", "side": "BUY", "title": "Test", "price": 0.5, "size": 1}]), encoding="utf-8")
    with pytest.raises(ValueError, match="wallet address is required"):
        load_wallet_activities(bare_path, wallet=None)


def test_analyze_wallet_forensics_empty_is_unknown():
    report = analyze_wallet_forensics("0x" + "5" * 40, [])
    assert report.classification == "unknown"
    assert report.confidence == 0.0


def test_wallet_forensics_cli_json_output(capsys):
    fixture = FIXTURES / "market_maker_wallet.json"
    payload = wallet_forensics_cli(input_json=str(fixture), as_json=True)
    captured = capsys.readouterr().out
    printed = json.loads(captured)
    assert payload["accepted"] is True
    assert printed["classification"] == payload["classification"]
    assert printed["metrics"]["merge_count"] == payload["metrics"]["merge_count"]
    assert printed["schema_version"] == SCHEMA_VERSION


def test_wallet_forensics_cli_text_output(capsys):
    fixture = FIXTURES / "directional_wallet.json"
    payload = wallet_forensics_cli(input_json=str(fixture), as_json=False)
    output = capsys.readouterr().out
    assert payload["accepted"] is True
    assert "Wallet Forensics Report" in output
    assert "quantitative_directional" in output
    assert "Reasons:" in output
    assert "Signals:" in output
    assert "Key Metrics:" in output


def test_wallet_forensics_cli_missing_input_graceful(capsys):
    payload = wallet_forensics_cli(input_json="/tmp/does-not-exist-polylens-forensics.json", as_json=True)
    output = capsys.readouterr().out
    assert payload["accepted"] is False
    assert "error" in payload
    assert "error" in json.loads(output)


def test_wallet_forensics_cli_main_dispatch(capsys):
    from src.cli import main

    fixture = FIXTURES / "arbitrage_wallet.json"
    sys.argv = ["polylens", "wallet-forensics", "--input-json", str(fixture), "--json"]
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["accepted"] is True
    assert output["classification"] == "arbitrage_trader"
    assert output["schema_version"] == SCHEMA_VERSION


def test_load_wallet_activities_missing_file():
    with pytest.raises(FileNotFoundError):
        load_wallet_activities("/tmp/does-not-exist-polylens-forensics.json", wallet="0x" + "d" * 40)


def test_report_json_is_deterministic():
    fixture = FIXTURES / "market_maker_wallet.json"
    first = build_wallet_forensics_report(fixture).to_json()
    second = build_wallet_forensics_report(fixture).to_json()
    assert first == second


def test_volume_metrics_by_asset():
    wallet = "0x" + "6" * 40
    activities = parse_activity_payload(
        [
            _activity(title="Bitcoin Up or Down", usdcSize=100.0, size=100.0),
            _activity(title="Ethereum Up or Down", usdcSize=50.0, size=50.0),
            _activity(title="Random politics market", usdcSize=25.0, size=25.0),
        ],
        wallet,
    )
    metrics = compute_basic_metrics(activities)
    assert metrics["btc_volume"] == 100.0
    assert metrics["eth_volume"] == 50.0
    assert metrics["other_volume"] == 25.0


def test_parse_activity_record_handles_missing_and_invalid_numeric_fields():
    wallet = "0x" + "7" * 40
    parsed = parse_activity_record(
        {
            "type": "TRADE",
            "side": "BUY",
            "outcome": "Yes",
            "title": "Test market",
            "price": "bad",
            "size": None,
            "timestamp": "",
        },
        wallet,
    )
    assert parsed is not None
    assert parsed.price_cents == 0.0
    assert parsed.shares == 0.0
    assert parsed.amount_usd == 0.0
    assert parsed.timestamp == 0.0


def test_outcome_normalization_case_and_index_fallback():
    wallet = "0x" + "8" * 40
    assert parse_activity_record(_activity(outcome="YES"), wallet).side == "yes"
    assert parse_activity_record(_activity(outcome=" No "), wallet).side == "no"
    assert parse_activity_record(_activity(outcome="UP"), wallet).side == "up"
    indexed = parse_activity_record(
        {
            "type": "TRADE",
            "side": "BUY",
            "outcome": "",
            "outcomeIndex": 1,
            "title": "Bitcoin Up or Down - 5m",
            "price": 0.4,
            "size": 1,
            "timestamp": 1,
        },
        wallet,
    )
    assert indexed is not None and indexed.side == "down"
    redeem_index = parse_activity_record(
        {"type": "REDEEM", "outcomeIndex": 999, "title": "Bitcoin Up or Down", "timestamp": 1},
        wallet,
    )
    assert redeem_index is not None and redeem_index.side == ""


def test_overlap_groups_by_condition_id_despite_title_differences():
    wallet = "0x" + "9" * 40
    activities = parse_activity_payload(
        [
            _activity(conditionId="0xsame", title="Bitcoin Up or Down - window A", outcome="Up"),
            _activity(conditionId="0xsame", title="BTC up/down window A", outcome="Down"),
        ],
        wallet,
    )
    metrics = compute_basic_metrics(activities)
    assert metrics["overlap_count"] == 1
    assert metrics["markets_traded"] == 1


def test_overlap_with_many_buys_and_sells_same_market():
    wallet = "0x" + "a" * 40
    rows = []
    for _ in range(5):
        rows.append(_activity(conditionId="0xbusy", title="Busy market", outcome="Up", side="BUY"))
    for _ in range(3):
        rows.append(_activity(conditionId="0xbusy", title="Busy market", outcome="Down", side="BUY"))
    rows.append(_activity(conditionId="0xbusy", title="Busy market", outcome="Up", side="SELL"))
    rows.append(_activity(conditionId="0xbusy", title="Busy market", outcome="Down", side="SELL"))
    metrics = compute_basic_metrics(parse_activity_payload(rows, wallet))
    assert metrics["overlap_count"] == 1
    assert metrics["buy_count"] == 8
    assert metrics["sell_count"] == 2


def test_arbitrage_ignores_zero_price_buys():
    wallet = "0x" + "b" * 40
    activities = [
        WalletActivity(wallet, "M", "OTHER", "yes", "buy", 0.0, 10, 0.0, 1, market_id="0xm"),
        WalletActivity(wallet, "M", "OTHER", "no", "buy", 48.0, 10, 4.8, 2, market_id="0xm"),
    ]
    metrics = compute_arbitrage_metrics(activities)
    assert metrics["arbitrage_opportunities"] == 0
    assert metrics["minimum_sum_price"] is None


def test_report_json_sorted_metrics_and_schema_version():
    fixture = FIXTURES / "market_maker_wallet.json"
    payload = json.loads(build_wallet_forensics_report(fixture).to_json())
    assert payload["schema_version"] == SCHEMA_VERSION
    metric_keys = list(payload["metrics"].keys())
    assert metric_keys == sorted(metric_keys)


def test_mixed_classification_when_mm_behavior_also_has_arbitrage_edge():
    wallet = "0x" + "c" * 40
    rows = []
    for idx in range(3):
        cid = f"0xmm{idx}"
        rows.extend(
            [
                _activity(conditionId=cid, title=f"BTC window {idx}", outcome="Up", price=0.42, usdcSize=42.0, size=100),
                _activity(conditionId=cid, title=f"BTC window {idx}", outcome="Down", price=0.50, usdcSize=50.0, size=100),
                _activity(conditionId=cid, type="MERGE", outcome="", side="", usdcSize=100.0, size=100, timestamp=1000 + idx),
                _activity(conditionId=cid, type="REDEEM", outcome="", side="", usdcSize=95.0, size=95, timestamp=1100 + idx),
            ]
        )
    report = analyze_wallet_forensics(wallet, parse_activity_payload(rows, wallet))
    assert report.classification in {"market_maker", "mixed", "arbitrage_trader"}
    assert report.metrics["overlap_count"] >= 3
    assert report.metrics["arbitrage_opportunities"] >= 2
