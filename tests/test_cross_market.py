from src.analysis.cross_market import compare_wallet_markets_to_kalshi


def test_cross_market_detects_conservative_crypto_overlap():
    trades = [
        {
            "conditionId": "pm-btc-100k",
            "title": "Will Bitcoin hit $100,000 in 2026?",
            "slug": "bitcoin-100000-2026",
            "outcome": "Yes",
            "size": 10,
            "price": 0.5,
        }
    ]
    kalshi = [
        {
            "ticker": "KXBTC-100K-26",
            "title": "Will Bitcoin be above $100,000 in 2026?",
            "subtitle": "Bitcoin price market",
            "rules_primary": "This market resolves yes if BTC trades above $100,000 in 2026.",
            "category": "Crypto",
        },
        {
            "ticker": "KXNFL-26",
            "title": "Will the Chiefs win the Super Bowl?",
            "category": "Sports",
        },
    ]
    candidates = compare_wallet_markets_to_kalshi(trades, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["kalshi_ticker"] == "KXBTC-100K-26"
    assert candidates[0]["confidence_band"] in {"medium", "high"}
    assert "bitcoin" in candidates[0]["shared_keywords_entities"]
    assert candidates[0]["reason"]


def test_cross_market_rejects_weak_cross_category_match():
    trades = [{"conditionId": "pm-nba", "title": "Will the Lakers win tonight?", "slug": "nba-lakers", "size": 1, "price": 1}]
    kalshi = [{"ticker": "KXBTC", "title": "Will Bitcoin rise tonight?", "category": "Crypto"}]
    assert compare_wallet_markets_to_kalshi(trades, kalshi) == []
