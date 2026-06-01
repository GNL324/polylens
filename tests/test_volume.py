from src.analysis.volume import summarize_volume


def test_summarize_volume_uses_usdc_size_and_price_size_fallback():
    trades = [
        {"usdcSize": 10, "size": 100, "price": 0.5, "title": "small"},
        {"size": 50, "price": 0.4, "title": "large"},
    ]
    summary = summarize_volume(trades)
    assert summary["trade_count"] == 2
    assert summary["total_volume"] == 30
    assert summary["average_trade_size"] == 15
    assert summary["largest_trade"]["title"] == "large"
