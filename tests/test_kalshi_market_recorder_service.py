from pathlib import Path


def test_kalshi_market_recorder_service_file_exists_and_command_is_safe():
    service = Path("deploy/systemd/kalshi-market-recorder.service")
    assert service.exists()
    text = service.read_text(encoding="utf-8")
    assert "WorkingDirectory=/home/noel/polylens" in text
    assert "EnvironmentFile=/home/noel/polylens/deploy/systemd/kalshi-market-recorder.env" in text
    assert "Restart=always" in text
    assert "RestartSec=10" in text
    assert "python -m src.cli kalshi-record-markets" in text
    assert "--assets ${KALSHI_RECORDER_ASSETS}" in text
    assert "--market-types ${KALSHI_RECORDER_MARKET_TYPES}" in text
    assert "--interval ${KALSHI_RECORDER_INTERVAL}" in text
    assert "--limit ${KALSHI_RECORDER_LIMIT}" in text
    assert "--discovery-limit ${KALSHI_RECORDER_DISCOVERY_LIMIT}" in text
    assert "--db-path ${KALSHI_RECORDER_DB_PATH}" in text
    assert "place_order" not in text
    assert "cancel_order" not in text


def test_kalshi_market_recorder_env_example_defaults_are_safe():
    env = Path("deploy/systemd/kalshi-market-recorder.env.example")
    assert env.exists()
    text = env.read_text(encoding="utf-8")
    assert "KALSHI_RECORDER_ASSETS=BTC,ETH,SOL" in text
    assert "KALSHI_RECORDER_MARKET_TYPES=crypto" in text
    assert "KALSHI_RECORDER_INTERVAL=60" in text
    assert "KALSHI_RECORDER_LIMIT=50" in text
    assert "KALSHI_RECORDER_DISCOVERY_LIMIT=500" in text
    assert "KALSHI_RECORDER_DB_PATH=data/kalshi_market_data.db" in text
    assert "LIVE_TRADING=false" in text
    assert "DRY_RUN=true" in text
