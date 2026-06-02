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
