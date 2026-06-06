from __future__ import annotations
import time
from src.adapters.crypto_price_feed import PriceTick, CryptoPriceFeedManager


def test_tick_staleness():
    recent_tick = PriceTick(source="test", symbol="BTC", timestamp=time.time() - 0.05)
    assert recent_tick.is_stale(max_age=1.0) is False

    old_tick = PriceTick(source="test", symbol="BTC", timestamp=time.time() - 5.0)
    assert old_tick.is_stale(max_age=1.0) is True


def test_manager_get_latest_returns_none_when_no_feed():
    mgr = CryptoPriceFeedManager(symbols=["BTC"])
    assert mgr.get_latest("BTC") is None
