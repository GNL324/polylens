from src.analysis.arb_pricing import extract_kalshi_pricing, polymarket_implied_yes_price, price_candidate


def test_kalshi_price_field_parsing():
    market = {
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4500",
        "no_bid_dollars": "0.5400",
        "no_ask_dollars": "0.5700",
        "last_price_dollars": "0.4400",
        "liquidity_dollars": "125.50",
    }
    pricing = extract_kalshi_pricing(market)
    assert pricing["yes_bid"] == 0.42
    assert pricing["yes_ask"] == 0.45
    assert pricing["no_ask"] == 0.57
    assert pricing["last_price"] == 0.44
    assert pricing["liquidity"] == 125.5


def test_polymarket_price_normalization_for_no_outcome():
    assert polymarket_implied_yes_price({"price": 0.37, "outcome": "Down", "outcomeIndex": 1}) == 0.63
    assert polymarket_implied_yes_price({"price": "42", "outcome": "Yes", "outcomeIndex": 0}) == 0.42


def test_yes_no_arbitrage_math_detects_theoretical_edge():
    candidate = {"polymarket_id": "pm1", "kalshi_ticker": "KX1", "polymarket_title": "A", "kalshi_title": "A"}
    trades = [{"conditionId": "pm1", "price": 0.4, "outcome": "Yes", "outcomeIndex": 0, "timestamp": 1, "size": 10}]
    kalshi = {"ticker": "KX1", "yes_ask_dollars": "0.7000", "no_ask_dollars": "0.5500", "yes_bid_dollars": "0.6900", "last_price_dollars": "0.6900", "liquidity_dollars": "10.0000"}
    priced = price_candidate(candidate, trades, kalshi)
    assert priced["theoretical_yes_no_arbitrage_exists"] is True
    assert priced["arbitrage_direction"] == "buy_polymarket_yes_buy_kalshi_no"
    assert priced["estimated_edge"] == 0.05


def test_missing_data_returns_insufficient_pricing_without_fake_edge():
    candidate = {"polymarket_id": "pm1", "kalshi_ticker": "KX1", "polymarket_title": "A", "kalshi_title": "A"}
    priced = price_candidate(candidate, [], {"ticker": "KX1", "title": "A"})
    assert priced["pricing_status"] == "insufficient pricing data"
    assert priced["estimated_edge"] is None
    assert priced["theoretical_yes_no_arbitrage_exists"] is False
    assert "insufficient pricing data" in priced["pricing_reason"]
