import os
import stat
from pathlib import Path

from src.cli import _env_float, _env_int, _env_str

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_service_files_exist():
    assert (SYSTEMD / "polylens-live-arb.service").exists()
    assert (SYSTEMD / "polylens-live-arb.env.example").exists()
    assert (SYSTEMD / "install_polylens_service.sh").exists()
    assert (SYSTEMD / "uninstall_polylens_service.sh").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper.service").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper.timer").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper.env.example").exists()


def test_service_command_is_correct():
    text = (SYSTEMD / "polylens-live-arb.service").read_text()
    assert "WorkingDirectory=/home/noel/polylens" in text
    assert "EnvironmentFile=/home/noel/polylens/deploy/systemd/polylens-live-arb.env" in text
    assert "Restart=always" in text
    assert "python -m src.cli watch-live-arb --save --interval 60 --min-score 0.7" in text


def test_env_example_contains_defaults():
    text = (SYSTEMD / "polylens-live-arb.env.example").read_text()
    assert "ODDS_API_KEY=" in text
    assert "POLYLENS_WEBHOOK_URL=" in text
    assert "POLYLENS_INTERVAL=60" in text
    assert "POLYLENS_MIN_SCORE=0.7" in text
    assert "POLYLENS_MIN_EDGE=0.01" in text
    assert "POLYLENS_DB_PATH=/home/noel/polylens/data/polylens.db" in text


def test_short_crypto_paper_service_is_paper_only():
    text = (SYSTEMD / "polylens-short-crypto-paper.service").read_text()
    assert "WorkingDirectory=/home/noel/polylens" in text
    assert "User=noel" in text
    assert "EnvironmentFile=-/home/noel/polylens/deploy/systemd/polylens-short-crypto-paper.env" in text
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text
    assert "/home/noel/.venv/bin/python -m src.cli short-crypto-paper-run --venues polymarket --assets BTC --windows 5 --max-trades 3 --min-edge -1 --json" in text
    assert "POLYLENS_LIVE_TRADING=false" in text
    assert "POLYLENS_AUTONOMOUS_CRYPTO=false" in text
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in text
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in text
    assert "trade-short-crypto" not in text
    assert "--live" not in text


def test_short_crypto_paper_timer_schedule():
    text = (SYSTEMD / "polylens-short-crypto-paper.timer").read_text()
    assert "OnBootSec=2min" in text
    assert "OnUnitActiveSec=5min" in text
    assert "Persistent=true" in text
    assert "Unit=polylens-short-crypto-paper.service" in text
    assert "WantedBy=timers.target" in text


def test_short_crypto_paper_env_example_disables_live_flags():
    text = (SYSTEMD / "polylens-short-crypto-paper.env.example").read_text()
    assert "PYTHONPATH=/home/noel/polylens" in text
    assert "POLYLENS_LIVE_TRADING=false" in text
    assert "POLYLENS_AUTONOMOUS_CRYPTO=false" in text
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in text
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in text


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("POLYLENS_INTERVAL", "45")
    monkeypatch.setenv("POLYLENS_MIN_SCORE", "0.8")
    monkeypatch.setenv("POLYLENS_DB_PATH", "/tmp/polylens.db")
    assert _env_int("POLYLENS_INTERVAL", 60) == 45
    assert _env_float("POLYLENS_MIN_SCORE") == 0.8
    assert _env_str("POLYLENS_DB_PATH") == "/tmp/polylens.db"


def test_scripts_are_executable():
    for name in ("install_polylens_service.sh", "uninstall_polylens_service.sh"):
        mode = os.stat(SYSTEMD / name).st_mode
        assert mode & stat.S_IXUSR
