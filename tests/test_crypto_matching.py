from src.analysis.crypto_matching import structured_crypto_candidates
from src.analysis.crypto_parser import parse_crypto_market_text


def test_crypto_parser_extracts_bitcoin_target_fields():
    parsed = parse_crypto_market_text("Will Bitcoin hit $100k by June 30?")
    assert parsed.asset_symbol == "BTC"
    assert parsed.asset_name == "Bitcoin"
    assert parsed.target_price == 100000
    assert parsed.direction == "touches"
    assert parsed.expiry_date == "2026-06-30"
    assert parsed.market_type == "price target"


def test_crypto_matching_btc_above_price_target_match():
    pm = [{"conditionId": "pm-btc", "title": "Will Bitcoin hit $100k by June 30?", "size": 10, "price": 0.4}]
    kalshi = [{"ticker": "KXBTC-100K-JUN30", "title": "Will BTC be above $100,000 on June 30?", "close_time": "2026-06-30T23:59:00Z"}]
    candidates = structured_crypto_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["structured_match"]["polymarket"]["asset_symbol"] == "BTC"
    assert candidates[0]["confidence_band"] in {"medium", "high"}


def test_crypto_matching_eth_below_price_target_match():
    pm = [{"conditionId": "pm-eth", "title": "Ethereum below $4,000 on July 31?", "size": 10, "price": 0.4}]
    kalshi = [{"ticker": "KXETH-4000-JUL31", "title": "Will ETH be below $4,000 on July 31?", "close_time": "2026-07-31T23:59:00Z"}]
    candidates = structured_crypto_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["structured_match"]["kalshi"]["direction"] == "below"


def test_crypto_matching_sol_range_market_match():
    pm = [{"conditionId": "pm-sol", "title": "Solana between $150 and $200 on August 15?", "size": 10, "price": 0.4}]
    kalshi = [{"ticker": "KXSOL-150-200-AUG15", "title": "Will SOL be between $150 and $200 on August 15?", "close_time": "2026-08-15T23:59:00Z"}]
    candidates = structured_crypto_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["structured_match"]["polymarket"]["market_type"] == "range"


def test_crypto_matching_rejects_mismatched_asset():
    pm = [{"conditionId": "pm-btc", "title": "Bitcoin above $100,000 on June 30?", "size": 1, "price": 0.4}]
    kalshi = [{"ticker": "KXETH-100K-JUN30", "title": "Will ETH be above $100,000 on June 30?", "close_time": "2026-06-30T23:59:00Z"}]
    assert structured_crypto_candidates(pm, kalshi) == []


def test_crypto_matching_rejects_mismatched_target_price():
    pm = [{"conditionId": "pm-btc", "title": "Bitcoin above $100,000 on June 30?", "size": 1, "price": 0.4}]
    kalshi = [{"ticker": "KXBTC-120K-JUN30", "title": "Will BTC be above $120,000 on June 30?", "close_time": "2026-06-30T23:59:00Z"}]
    assert structured_crypto_candidates(pm, kalshi) == []


def test_crypto_matching_rejects_mismatched_expiry():
    pm = [{"conditionId": "pm-btc", "title": "Bitcoin above $100,000 on June 30?", "size": 1, "price": 0.4}]
    kalshi = [{"ticker": "KXBTC-100K-DEC31", "title": "Will BTC be above $100,000 on December 31?", "close_time": "2026-12-31T23:59:00Z"}]
    assert structured_crypto_candidates(pm, kalshi) == []


def test_crypto_matching_rejects_ambiguous_missing_target():
    pm = [{"conditionId": "pm-btc", "title": "Will Bitcoin move a lot by June 30?", "size": 1, "price": 0.4}]
    kalshi = [{"ticker": "KXBTC", "title": "Will BTC be above $100,000 on June 30?", "close_time": "2026-06-30T23:59:00Z"}]
    assert structured_crypto_candidates(pm, kalshi) == []
