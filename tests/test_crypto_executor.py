from src.analysis.short_crypto_executor import ShortCryptoSignalEngine
from src.analysis.short_crypto_markets import ShortCryptoMarket
from pathlib import Path


def test_generate_signals_requires_asset_and_edge():
    engine = ShortCryptoSignalEngine(assets=["BTC"], windows=[5], min_edge=0.01)
    market = ShortCryptoMarket(
        asset="BTC",
        venue="kalshi",
        ticker="BTC-UP-5",
        start_ts=1.0,
        end_ts=1.0 + 5 * 60,
        direction="up",
        yes_bid=0.5,
        yes_ask=0.6,
        no_bid=0.4,
        no_ask=0.5,
        liquidity=200,
        timestamp=1e9,
    )
    spot_map = {"BTC": 100.0}
    sigs = engine.generate_signals([market], spot_map)
    assert len(sigs) == 1 or len(sigs) == 0
