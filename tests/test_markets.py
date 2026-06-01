from src.analysis.markets import infer_category, summarize_markets


def test_market_summary_counts_favorites_and_categories():
    trades = [
        {"conditionId": "a", "title": "Will Bitcoin hit 100k?", "size": 10, "price": 0.5},
        {"conditionId": "a", "title": "Will Bitcoin hit 100k?", "size": 10, "price": 0.5},
        {"conditionId": "b", "title": "NBA Finals winner", "size": 4, "price": 0.5},
    ]
    summary = summarize_markets(trades)
    assert summary["market_count"] == 2
    assert summary["favorite_markets"][0]["market"] == "Will Bitcoin hit 100k?"
    assert infer_category(trades[0]) == "Crypto"
    assert {item["category"] for item in summary["favorite_categories"]} >= {"Crypto", "Sports"}
