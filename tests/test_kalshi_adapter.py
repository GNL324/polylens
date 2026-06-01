from pathlib import Path
from unittest.mock import patch

from src.adapters.kalshi import KalshiClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'{"markets":[{"ticker":"KXBTC-26","title":"Will Bitcoin be above $100,000?"}],"cursor":""}'


def test_kalshi_client_fetches_and_caches_markets(tmp_path):
    client = KalshiClient(raw_dir=tmp_path)
    with patch("src.adapters.kalshi.urlopen", return_value=FakeResponse()):
        markets = client.get_markets(max_pages=1)
    assert markets[0]["ticker"] == "KXBTC-26"
    assert (tmp_path / "kalshi_markets_cache.json").exists()
    assert list(Path(tmp_path).glob("kalshi_markets_page0_raw.json"))


def test_kalshi_client_uses_cache_on_failure(tmp_path):
    (tmp_path / "kalshi_markets_cache.json").write_text('{"markets":[{"ticker":"CACHE","title":"Cached market"}]}')
    client = KalshiClient(raw_dir=tmp_path)
    with patch("src.adapters.kalshi.urlopen", side_effect=TimeoutError("offline")):
        markets = client.get_markets(max_pages=1)
    assert markets == [{"ticker": "CACHE", "title": "Cached market"}]
