from src.analysis.kalshi_market_recorder import record_kalshi_markets, summarize_kalshi_market_data
from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthenticatedClient


class FakeKalshiClient:
    def get_markets(self, status="open", limit=100, max_pages=1, series_ticker=None):
        return [
            {"ticker": "KXBTC15M", "title": "Bitcoin up in 15 minutes", "category": "Crypto", "yes_bid": 45, "yes_ask": 47, "no_bid": 53, "no_ask": 55, "liquidity": 1000, "volume": 2500, "close_time": "2026-06-04T12:00:00Z"},
            {"ticker": "KXNBA", "title": "NBA Finals", "category": "Sports", "yes_bid": 30, "yes_ask": 35},
        ][:limit]

    def get_orderbook(self, ticker):
        return {"orderbook": {"yes": [[45, 10], [44, 8]], "yes_asks": [[47, 12]], "no": [[53, 9]], "no_asks": [[55, 11]]}}


def test_record_kalshi_markets_saves_snapshots(tmp_path):
    db = tmp_path / "kalshi.db"
    result = record_kalshi_markets(FakeKalshiClient(), assets={"BTC"}, market_types={"crypto"}, interval=1, duration_minutes=None, limit=10, db_path=str(db))
    assert result["market_snapshots_saved"] == 1
    assert result["orderbook_snapshots_saved"] == 1
    summary = summarize_kalshi_market_data(str(db))
    assert summary["snapshots_collected"] == 1
    assert summary["orderbook_snapshots_collected"] == 1
    assert summary["assets_tracked"] == ["BTC"]
    assert summary["average_spread"] == 0.02
    assert summary["most_liquid_markets"][0]["ticker"] == "KXBTC15M"


def test_data_summary_reports_missing_fields(tmp_path):
    class MissingClient(FakeKalshiClient):
        def get_markets(self, status="open", limit=100, max_pages=1, series_ticker=None):
            return [{"ticker": "KXETH", "title": "Ethereum price", "category": "Crypto"}]

    db = tmp_path / "kalshi.db"
    record_kalshi_markets(MissingClient(), assets={"ETH"}, market_types={"crypto"}, include_orderbooks=False, db_path=str(db))
    summary = summarize_kalshi_market_data(str(db))
    assert summary["missing_fields"]["missing_yes_bid"] == 1
    assert summary["missing_fields"]["missing_liquidity"] == 1


def test_write_endpoints_still_blocked(tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    client = KalshiAuthenticatedClient(KalshiAuthConfig(api_key_id="id", private_key_path=key_path, env="production"), raw_dir=tmp_path)
    assert client.place_order()["mode"] == "write_blocked"
    assert client.cancel_order("order")["mode"] == "write_blocked"


def test_kalshi_record_and_summary_cli_smoke(tmp_path, monkeypatch):
    from src import cli

    monkeypatch.setattr(cli, "KalshiClient", lambda raw_dir="data/raw": FakeKalshiClient())
    db = tmp_path / "kalshi.db"
    recorded = cli.kalshi_record_markets(assets="BTC", market_types="crypto", interval=1, duration_minutes=None, limit=10, db_path=str(db), as_json=True)
    assert recorded["market_snapshots_saved"] == 1
    summary = cli.kalshi_data_summary(db_path=str(db), as_json=True)
    assert summary["snapshots_collected"] == 1

class PaginatedKalshiClient:
    def __init__(self):
        self.calls = []

    def get_markets(self, status="open", limit=100, max_pages=1, series_ticker=None):
        self.calls.append({"limit": limit, "max_pages": max_pages, "series_ticker": series_ticker})
        markets = [
            {"ticker": f"KXSPORT{i}", "title": "Unrelated sports market", "category": "Sports", "yes_bid": 40, "yes_ask": 45}
            for i in range(20)
        ]
        markets.extend([
            {"ticker": "KXBTC15M-1", "event_ticker": "KXBTC15M", "title": "Bitcoin up", "category": "Crypto", "yes_bid": 45, "yes_ask": 47, "liquidity": 1000},
            {"ticker": "KXETH15M-1", "event_ticker": "KXETH15M", "title": "Ethereum up", "category": "Crypto", "yes_bid": 50, "yes_ask": 52, "liquidity": 900},
            {"ticker": "KXSOLANA15M-1", "event_ticker": "KXSOLANA15M", "title": "Solana up", "category": "Crypto", "yes_bid": 55, "yes_ask": 57, "liquidity": 800},
            {"ticker": "KXMVESPORTSMULTIGAMEEXTENDED-ETH", "event_ticker": "KXMVE", "title": "Bundle with Ethereum", "category": "Crypto", "yes_bid": 10, "yes_ask": 90},
        ])
        if series_ticker:
            markets = [market for market in markets if str(market.get("ticker", "")).startswith(series_ticker) or str(market.get("event_ticker", "")).startswith(series_ticker)]
        return markets[: min(len(markets), limit * max_pages)]

    def get_orderbook(self, ticker):
        return {"orderbook": {"yes": [[45, 10]], "yes_asks": [[47, 12]]}}


def test_discovery_limit_finds_crypto_after_non_crypto(tmp_path):
    client = PaginatedKalshiClient()
    db = tmp_path / "kalshi.db"
    result = record_kalshi_markets(client, assets={"BTC", "ETH", "SOL"}, market_types={"crypto"}, limit=2, discovery_limit=50, db_path=str(db))
    assert result["market_snapshots_saved"] == 2
    assert any(call.get("series_ticker") == "KXBTC" for call in client.calls)
    summary = summarize_kalshi_market_data(str(db))
    assert set(summary["assets_tracked"]) == {"BTC", "ETH"}
    assert summary["snapshots_collected"] == 2


def test_ticker_prefix_filter_limits_matches(tmp_path):
    db = tmp_path / "kalshi.db"
    result = record_kalshi_markets(PaginatedKalshiClient(), assets={"BTC", "ETH", "SOL"}, market_types={"crypto"}, ticker_prefix="KXETH", limit=10, discovery_limit=50, db_path=str(db))
    assert result["market_snapshots_saved"] == 1
    assert result["summary"]["last_recorded_tickers"] == ["KXETH15M-1"]


def test_event_ticker_prefix_filter_limits_matches(tmp_path):
    db = tmp_path / "kalshi.db"
    result = record_kalshi_markets(PaginatedKalshiClient(), assets={"BTC", "ETH", "SOL"}, market_types={"crypto"}, event_ticker_prefix="KXSOLANA", limit=10, discovery_limit=50, db_path=str(db))
    assert result["market_snapshots_saved"] == 1
    assert result["summary"]["assets_tracked"] == ["SOL"]


def test_bundle_markets_are_rejected_for_asset_recording(tmp_path):
    db = tmp_path / "kalshi.db"
    result = record_kalshi_markets(PaginatedKalshiClient(), assets={"ETH"}, market_types={"crypto"}, limit=10, discovery_limit=50, db_path=str(db))
    assert result["market_snapshots_saved"] == 1
    assert result["summary"]["last_recorded_tickers"] == ["KXETH15M-1"]
