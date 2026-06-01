from src.analysis.arb_signals import classify_behavior
from src.analysis.markets import summarize_markets
from src.analysis.volume import summarize_volume


def test_classification_detects_market_maker():
    trades = []
    for i in range(60):
        trades.append({"conditionId": f"m{i % 10}", "side": "BUY" if i // 10 % 2 else "SELL", "outcome": "Yes", "title": "Election market", "size": 10, "price": 0.5})
    volume = summarize_volume(trades)
    markets = summarize_markets(trades)
    result = classify_behavior(trades, [], markets, volume)
    assert result["behavior_classification"] == "Market Maker"
    assert result["signals"]["market_making"] is True


def test_classification_detects_crypto_specialist():
    trades = [{"conditionId": f"c{i}", "side": "BUY", "outcome": "Yes", "title": "Bitcoin weekly price", "size": 10, "price": 0.5} for i in range(8)]
    result = classify_behavior(trades, [], summarize_markets(trades), summarize_volume(trades))
    assert result["behavior_classification"] == "Crypto Specialist"
