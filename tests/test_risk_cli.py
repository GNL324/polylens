from src.cli import risk_events, risk_halt, risk_resume, risk_status
from src.risk import RiskEngine, RiskRequest


def test_risk_status_cli_smoke(tmp_path, capsys):
    db_path = tmp_path / "risk.db"
    status = risk_status(db_path=str(db_path))
    output = capsys.readouterr().out
    assert status["mode"] == "DRY RUN"
    assert "Polylens Risk Status" in output
    assert "LIVE_TRADING: False" in output


def test_risk_events_cli_smoke(tmp_path, capsys):
    db_path = tmp_path / "risk.db"
    RiskEngine(db_path).evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="a", proposed_stake=1))
    rows = risk_events(limit=5, db_path=str(db_path))
    output = capsys.readouterr().out
    assert len(rows) == 1
    assert "APPROVE" in output


def test_risk_halt_resume_cli_smoke(tmp_path, capsys):
    db_path = tmp_path / "risk.db"
    halted = risk_halt(reason="manual test", db_path=str(db_path))
    resumed = risk_resume(db_path=str(db_path))
    output = capsys.readouterr().out
    assert halted["active"] is True
    assert resumed["resumed_halts"] == 1
    assert "manual test" in output
