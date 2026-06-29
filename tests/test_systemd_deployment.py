import os
import stat
import subprocess
from pathlib import Path

import pytest

from src.cli import _env_float, _env_int, _env_str

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_service_files_exist():
    assert (SYSTEMD / "polylens-dashboard.service").exists()
    assert (SYSTEMD / "polylens-trader-dashboard.service").exists()
    assert (SYSTEMD / "polylens-live-arb.service").exists()
    assert (SYSTEMD / "polylens-live-arb.env.example").exists()
    assert (SYSTEMD / "install_polylens_service.sh").exists()
    assert (SYSTEMD / "uninstall_polylens_service.sh").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper.service").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper.timer").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper.env.example").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper-settle.service").exists()
    assert (SYSTEMD / "polylens-short-crypto-paper-settle.timer").exists()
    assert (SYSTEMD / "polylens-prop-arb-collector.service").exists()
    assert (SYSTEMD / "polylens-prop-arb-collector.timer").exists()
    assert (SYSTEMD / "polylens-prop-arb-collector.env.example").exists()
    assert (SYSTEMD / "polylens-prop-arb-collector-preflight.sh").exists()
    assert (SYSTEMD / "wallet-autonomy.service").exists()
    assert (SYSTEMD / "wallet-autonomy.timer").exists()
    assert (SYSTEMD / "polylens-telegram-console.service").exists()
    assert (SYSTEMD / "polylens-telegram-daily-report.service").exists()
    assert (SYSTEMD / "polylens-telegram-daily-report.timer").exists()
    assert (SYSTEMD / "polylens-telegram-sre-alert.service").exists()
    assert (SYSTEMD / "polylens-telegram-sre-alert.timer").exists()


def test_service_command_is_correct():
    text = (SYSTEMD / "polylens-live-arb.service").read_text()
    assert "WorkingDirectory=/home/noel/polylens" in text
    assert "EnvironmentFile=/home/noel/polylens/deploy/systemd/polylens-live-arb.env" in text
    assert "Restart=always" in text
    assert "python -m src.cli watch-live-arb --save --interval 60 --min-score 0.7" in text


def test_dashboard_service_topology_is_localhost_only():
    main_dashboard = (SYSTEMD / "polylens-dashboard.service").read_text()
    trader_dashboard = (SYSTEMD / "polylens-trader-dashboard.service").read_text()

    assert "User=noel" in main_dashboard
    assert "WorkingDirectory=/home/noel/polylens" in main_dashboard
    assert "Environment=PYTHONPATH=/home/noel/polylens" in main_dashboard
    assert "LIVE_TRADING=false" in main_dashboard
    assert "DRY_RUN=true" in main_dashboard
    assert "POLYLENS_LIVE_TRADING=false" in main_dashboard
    assert "POLYLENS_AUTONOMOUS_CRYPTO=false" in main_dashboard
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in main_dashboard
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in main_dashboard
    assert (
        "ExecStart=/home/noel/polylens/.venv/bin/python -m src.cli web-dashboard "
        "--host 127.0.0.1 --port 8787"
    ) in main_dashboard
    assert "0.0.0.0" not in main_dashboard

    assert "User=noel" in trader_dashboard
    assert "WorkingDirectory=/home/noel/polylens" in trader_dashboard
    assert "Environment=PYTHONPATH=/home/noel/polylens" in trader_dashboard
    assert "LIVE_TRADING=false" in trader_dashboard
    assert "DRY_RUN=true" in trader_dashboard
    assert "POLYLENS_LIVE_TRADING=false" in trader_dashboard
    assert "POLYLENS_AUTONOMOUS_CRYPTO=false" in trader_dashboard
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in trader_dashboard
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in trader_dashboard
    assert (
        "ExecStart=/home/noel/polylens/.venv/bin/python -m src.cli trader-dashboard "
        "--host 127.0.0.1 --port 8788"
    ) in trader_dashboard
    assert "0.0.0.0" not in trader_dashboard


def test_dashboard_topology_validator_ignores_peer_wildcard(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    validator = ROOT / "scripts" / "validate_dashboard_topology.sh"

    (fake_bin / "ss").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'sport = :8787'* ]]; then\n"
        "  echo 'LISTEN 0 2048 127.0.0.1:8787 0.0.0.0:*'\n"
        "elif [[ \"$*\" == *'sport = :8788'* ]]; then\n"
        "  echo 'LISTEN 0 2048 127.0.0.1:8788 0.0.0.0:*'\n"
        "else\n"
        "  echo 'LISTEN 0 2048 127.0.0.1:8787 0.0.0.0:*'\n"
        "  echo 'LISTEN 0 2048 127.0.0.1:8788 0.0.0.0:*'\n"
        "fi\n"
    )
    (fake_bin / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "url=\"${@: -1}\"\n"
        "if [[ \"$url\" == 'http://127.0.0.1:8787/' ]]; then\n"
        "  echo -n 307\n"
        "else\n"
        "  echo -n 200\n"
        "fi\n"
    )
    (fake_bin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'polylens-dashboard.service'* ]]; then\n"
        "  echo '/home/noel/polylens/.venv/bin/python -m src.cli web-dashboard --host 127.0.0.1 --port 8787'\n"
        "elif [[ \"$*\" == *'polylens-trader-dashboard.service'* ]]; then\n"
        "  echo '/home/noel/polylens/.venv/bin/python -m src.cli trader-dashboard --host 127.0.0.1 --port 8788'\n"
        "fi\n"
    )
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run([validator], cwd=ROOT, env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Main dashboard root redirects to /mission-control with HTTP 307" in result.stdout
    assert "Summary: 0 failure(s)" in result.stdout


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


def test_short_crypto_paper_settle_service_is_paper_only():
    text = (SYSTEMD / "polylens-short-crypto-paper-settle.service").read_text()
    assert "WorkingDirectory=/home/noel/polylens" in text
    assert "User=noel" in text
    assert "EnvironmentFile=-/home/noel/polylens/deploy/systemd/polylens-short-crypto-paper.env" in text
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text
    assert "/home/noel/.venv/bin/python -m src.cli short-crypto-paper-settle --json" in text
    assert "/home/noel/.venv/bin/python -m src.cli short-crypto-paper-report --json" in text
    assert "POLYLENS_LIVE_TRADING=false" in text
    assert "POLYLENS_AUTONOMOUS_CRYPTO=false" in text
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in text
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in text
    assert "trade-short-crypto" not in text
    assert "--live" not in text


def test_short_crypto_paper_settle_timer_schedule():
    text = (SYSTEMD / "polylens-short-crypto-paper-settle.timer").read_text()
    assert "OnBootSec=4min" in text
    assert "OnUnitActiveSec=5min" in text
    assert "Persistent=true" in text
    assert "Unit=polylens-short-crypto-paper-settle.service" in text
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
    for name in ("install_polylens_service.sh", "uninstall_polylens_service.sh", "polylens-prop-arb-collector-preflight.sh"):
        mode = os.stat(SYSTEMD / name).st_mode
        assert mode & stat.S_IXUSR


def test_prop_arb_collector_service_is_analytics_only():
    text = (SYSTEMD / "polylens-prop-arb-collector.service").read_text()
    assert "WorkingDirectory=/home/noel/polylens" in text
    assert "User=noel" in text
    assert "Type=oneshot" in text
    assert "EnvironmentFile=-/home/noel/polylens/deploy/systemd/polylens-prop-arb-collector.env" in text
    assert "ExecStartPre=/home/noel/polylens/deploy/systemd/polylens-prop-arb-collector-preflight.sh" in text
    assert "scan-prop-arb" in text
    assert "--summary-json" in text
    assert "--json" not in text.replace("--summary-json", "")
    assert "POLYLENS_LIVE_TRADING=false" in text
    assert "watch-prop-arb" not in text
    assert "trade-" not in text


def test_prop_arb_collector_timer_schedule():
    text = (SYSTEMD / "polylens-prop-arb-collector.timer").read_text()
    assert "OnUnitActiveSec=60" in text
    assert "Unit=polylens-prop-arb-collector.service" in text
    assert "WantedBy=timers.target" in text


def test_prop_arb_collector_env_example_requires_keys():
    text = (SYSTEMD / "polylens-prop-arb-collector.env.example").read_text()
    assert "ODDS_API_KEY=" in text
    assert "ODDSBLAZE_API_KEY=" in text
    assert "POLYLENS_LIVE_TRADING=false" in text


def test_wallet_autonomy_service_is_paper_only():
    service = (SYSTEMD / "wallet-autonomy.service").read_text()
    timer = (SYSTEMD / "wallet-autonomy.timer").read_text()
    assert "Type=oneshot" in service
    assert "POLYLENS_LIVE_TRADING=false" in service
    assert "LIVE_TRADING=false" in service
    assert "DRY_RUN=true" in service
    assert "wallet-service-run" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Unit=wallet-autonomy.service" in timer
    assert "trade-" not in service


def test_telegram_console_service_is_read_only_and_paper_only():
    service = (SYSTEMD / "polylens-telegram-console.service").read_text()
    assert "WorkingDirectory=/home/noel/polylens" in service
    assert "Environment=PYTHONPATH=/home/noel/polylens" in service
    assert "POLYLENS_TELEGRAM_PAPER_ONLY=true" in service
    assert "POLYLENS_TELEGRAM_LIVE_ENABLED=false" in service
    assert "POLYLENS_LIVE_TRADING=false" in service
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in service
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in service
    assert "telegram-console" in service
    assert "trade-" not in service
    assert "--live" not in service


def test_telegram_daily_report_service_and_timer_are_safe():
    service = (SYSTEMD / "polylens-telegram-daily-report.service").read_text()
    timer = (SYSTEMD / "polylens-telegram-daily-report.timer").read_text()
    assert "Type=oneshot" in service
    assert "WorkingDirectory=/home/noel/polylens" in service
    assert "Environment=PYTHONPATH=/home/noel/polylens" in service
    assert "POLYLENS_TELEGRAM_NOTIFICATIONS_ENABLED=true" in service
    assert "POLYLENS_TELEGRAM_DAILY_REPORT_ENABLED=true" in service
    assert "POLYLENS_TELEGRAM_PAPER_ONLY=true" in service
    assert "POLYLENS_TELEGRAM_LIVE_ENABLED=false" in service
    assert "LIVE_TRADING=false" in service
    assert "DRY_RUN=true" in service
    assert "POLYLENS_LIVE_TRADING=false" in service
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in service
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in service
    assert "telegram-daily-report" in service
    assert "trade-" not in service
    assert "--live" not in service
    assert "OnCalendar=*-*-* 08:00:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=polylens-telegram-daily-report.service" in timer


def test_telegram_sre_alert_service_and_timer_are_safe():
    service = (SYSTEMD / "polylens-telegram-sre-alert.service").read_text()
    timer = (SYSTEMD / "polylens-telegram-sre-alert.timer").read_text()
    assert "Type=oneshot" in service
    assert "WorkingDirectory=/home/noel/polylens" in service
    assert "Environment=PYTHONPATH=/home/noel/polylens" in service
    assert "POLYLENS_TELEGRAM_NOTIFICATIONS_ENABLED=true" in service
    assert "POLYLENS_TELEGRAM_SRE_ALERT_ENABLED=true" in service
    assert "POLYLENS_TELEGRAM_PAPER_ONLY=true" in service
    assert "POLYLENS_TELEGRAM_LIVE_ENABLED=false" in service
    assert "LIVE_TRADING=false" in service
    assert "DRY_RUN=true" in service
    assert "POLYLENS_LIVE_TRADING=false" in service
    assert "POLYLENS_KALSHI_LIVE_SENDS_ENABLED=false" in service
    assert "POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED=false" in service
    assert "telegram-sre-alert" in service
    assert "trade-" not in service
    assert "--live" not in service
    assert "OnCalendar=*-*-* *:0/15:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=polylens-telegram-sre-alert.service" in timer


AUTONOMOUS_SERVICE_FILES = [
    "polylens-dashboard.service",
    "polylens-trader-dashboard.service",
    "wallet-autonomy.service",
    "polylens-trader-signal-cycle.service",
    "polylens-paper-trading.service",
    "polylens-short-crypto-paper.service",
    "polylens-short-crypto-paper-settle.service",
    "polylens-prop-arb-collector.service",
    "polylens-live-arb.service",
    "polylens-prop-watch.service",
    "kalshi-market-recorder.service",
    "polylens-telegram-console.service",
    "polylens-telegram-daily-report.service",
    "polylens-telegram-sre-alert.service",
]


@pytest.mark.parametrize("service_name", AUTONOMOUS_SERVICE_FILES)
def test_autonomous_services_disable_live_trading(service_name):
    text = (SYSTEMD / service_name).read_text()
    assert "LIVE_TRADING=false" in text or "POLYLENS_LIVE_TRADING=false" in text
    assert "DRY_RUN=true" in text
    assert "trade-short-crypto" not in text
    assert "kalshi-live-trade" not in text
    assert "--live" not in text
