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


from src.analysis.crypto_matching import structured_crypto_candidates_with_diagnostics


def test_crypto_matching_btc_daily_up_down_match():
    pm = [{"conditionId": "pm-btc-day", "title": "Bitcoin up on June 30", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXBTCDAILY-20260630-UP", "title": "Will BTC be higher by close on June 30?", "close_time": "2026-06-30T21:00:00Z"}]
    candidates = structured_crypto_candidates(pm, kalshi)
    assert len(candidates) == 1
    parsed = candidates[0]["structured_match"]
    assert parsed["polymarket"]["window_type"] == "daily"
    assert parsed["kalshi"]["comparator_baseline"] == "previous close"


def test_crypto_matching_btc_hourly_up_down_match():
    pm = [{"conditionId": "pm-btc-hour", "title": "Bitcoin price up or down on June 30 5:00PM-6:00PM", "outcome": "Up", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXBTC-HOURLY", "title": "Will BTC be higher by close on June 30 6:00PM?", "close_time": "2026-06-30T22:00:00Z"}]
    candidates = structured_crypto_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["structured_match"]["polymarket"]["window_type"] == "hourly"


def test_crypto_matching_eth_weekly_up_down_match():
    pm = [{"conditionId": "pm-eth-week", "title": "Ethereum higher this week by close on July 3", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXETHWEEKLY", "title": "Will ETH be up this week?", "close_time": "2026-07-03T21:00:00Z"}]
    candidates = structured_crypto_candidates(pm, kalshi)
    assert len(candidates) == 1
    assert candidates[0]["structured_match"]["polymarket"]["window_type"] == "weekly"


def test_crypto_matching_rejects_fixed_target_vs_up_down():
    pm = [{"conditionId": "pm-btc-target", "title": "Bitcoin above $100,000 on June 30", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXBTCDAILY", "title": "Will BTC be higher by close on June 30?", "close_time": "2026-06-30T21:00:00Z"}]
    candidates, diagnostics = structured_crypto_candidates_with_diagnostics(pm, kalshi)
    assert candidates == []
    assert any("fixed-target" in item["reason"] for item in diagnostics)


def test_crypto_matching_rejects_non_overlapping_time_windows():
    pm = [{"conditionId": "pm-btc-day", "title": "Bitcoin up on June 30", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXBTCDAILY", "title": "Will BTC be higher by close on July 10?", "close_time": "2026-07-10T21:00:00Z"}]
    candidates, diagnostics = structured_crypto_candidates_with_diagnostics(pm, kalshi)
    assert candidates == []
    assert any("close windows do not overlap" in item["reason"] for item in diagnostics)


def test_crypto_diagnostics_include_rejection_reason():
    pm = [{"conditionId": "pm-btc", "title": "Bitcoin up today", "size": 1, "price": 0.5}]
    kalshi = [{"ticker": "KXETHDAILY", "title": "Will ETH be higher by close today?", "close_time": "2026-06-30T21:00:00Z"}]
    _candidates, diagnostics = structured_crypto_candidates_with_diagnostics(pm, kalshi)
    rejected = [item for item in diagnostics if not item["accepted"]]
    assert rejected
    assert rejected[0]["reason"].startswith("asset mismatch")
    assert rejected[0]["parsed_polymarket"]["asset_symbol"] == "BTC"
