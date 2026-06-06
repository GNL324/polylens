from src.trading.executor import ShortCryptoExecutor
from src.analysis.short_crypto_executor import CryptoSignal
from pathlib import Path
def test_kill_switch_blocks(monkeypatch, tmp_path):
    ks = tmp_path / ".kill_switch"
    ks.write_text("stop")
    ex = ShortCryptoExecutor(kill_switch=str(ks))
    sig = CryptoSignal(asset="BTC", window_minutes=5, direction="up", venue="kalshi", ticker="x", spot_price=100.0, implied_prob=0.55, model_prob=0.7, edge=0.15, roi=0.1)
    res = ex.execute({"asset":"BTC","direction":"up","roi":0.1,"spot_price":100}, mode="paper")
    assert res["accepted"] is False
    assert res["reason"] == "kill switch active"
