from pathlib import Path


def test_prop_watch_service_file_contents() -> None:
    service = Path("deploy/systemd/polylens-prop-watch.service").read_text(encoding="utf-8")

    assert "User=noel" in service
    assert "WorkingDirectory=/home/noel/polylens" in service
    assert "EnvironmentFile=/home/noel/polylens/deploy/systemd/polylens-live-arb.env" in service
    assert "Restart=always" in service
    assert "RestartSec=10" in service
    assert "ExecStart=/home/noel/.venv/bin/python -m src.cli watch-prop-arb --profile __ACTIVE__ --json" in service
