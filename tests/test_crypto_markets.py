from __future__ import annotations
import time
from src.analysis.short_crypto_markets import ShortCryptoMarket, normalize_short_crypto_markets


def _market(**overrides):
    base = dict(
        asset="BTC",
        venue="kalshi",
        ticker="BTC-UP-5",
        start_ts=time.time() - 60,
        end_ts=time.time() + 60,
        direction="up",
        yes_bid=0.5,
        yes_ask=0.6,
        no_bid=0.4,
        no_ask=0.5,
        liquidity=150.0,
        timestamp=time.time(),
    )
    base.update(overrides)
    return ShortCryptoMarket(**base)


def test_normalize_filters_non_crypto_and_direction():
    markets = normalize_short_crypto_markets(
        [
            {"title": "ETH-UP-5", "pricing": {"yes_bid": 0.5, "yes_ask": 0.6}, "ticker": "ETH-UP-5", "open_time": 0, "close_time": 60, "liquidity": 100},
            {"title": "BTC-FLAT-5", "pricing": {"yes_bid": 0.5, "yes_ask": 0.6}, "ticker": "BTC-FLAT-5", "open_time": 0, "close_time": 60, "liquidity": 100},
        ],
        [],
    )
    assert len(markets) == 1


def test_is_valid_rejects_stale_and_future():
    now = 1_000_000_000.0
    stale = _market(timestamp=now - 10.0)
    assert stale.is_valid(max_age=2.0, now=now) is False

    future = _market(timestamp=now + 20.0)
    assert future.is_valid(max_age=2.0, now=now) is False


def test_is_valid_accepts_fresh():
    now = 1_000_000_000.0
    fresh = _market(timestamp=now - 0.1)
    assert fresh.is_valid(max_age=2.0, now=now) is True


def test_is_valid_rejects_missing_timestamp():
    market = _market(timestamp=None)
    assert market.is_valid(max_age=2.0) is False


def test_is_valid_rejects_missing_yes_book():
    market = _market(yes_bid=None, yes_ask=None)
    assert market.is_valid(max_age=2.0) is False
