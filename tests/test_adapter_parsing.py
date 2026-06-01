from pathlib import Path
from unittest.mock import patch

from src.adapters.polymarket import PolymarketClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'[{"proxyWallet":"0xabc","size":1,"price":0.5}]'


def test_client_stores_raw_response(tmp_path):
    client = PolymarketClient(raw_dir=tmp_path)
    with patch("src.adapters.polymarket.urlopen", return_value=FakeResponse()):
        payload = client._get_json(client.data_base_url, "trades", {"user": "0x" + "1" * 40}, "wallet")
    assert payload[0]["size"] == 1
    assert list(Path(tmp_path).glob("wallet_trades_raw.json"))
