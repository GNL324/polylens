from src.risk import RiskConfig, RiskEngine, RiskRequest
from src.risk.models import RiskDecisionType


def test_risk_approval(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig(min_opportunity_score=0.5, min_expected_edge=0.01))
    decision = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="a", proposed_stake=5, score=0.9, edge=0.05))
    assert decision.decision == RiskDecisionType.APPROVE
    assert decision.approved is True


def test_max_stake_rejection(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig(max_position_size=10))
    decision = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="a", proposed_stake=11))
    assert decision.decision == RiskDecisionType.REJECT
    assert "max position size exceeded" in decision.reason


def test_max_daily_loss_halt(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig(max_daily_loss=25))
    engine.set_pnl(daily_pnl=-25)
    decision = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="a", proposed_stake=1))
    assert decision.decision == RiskDecisionType.HALT
    assert "max daily loss exceeded" in decision.reason


def test_global_manual_halt(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig())
    engine.halt("manual halt")
    decision = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="a", proposed_stake=1))
    assert decision.decision == RiskDecisionType.HALT
    assert "manual halt" in decision.reason
    assert engine.resume() == 1


def test_dry_run_live_protection_paper_only(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig(dry_run=False, live_trading=True))
    decision = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="a", proposed_stake=1))
    assert decision.decision == RiskDecisionType.PAPER_ONLY
    assert decision.approved is True
    assert "live execution requested but blocked" in decision.reason


def test_duplicate_suppression(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig(duplicate_opportunity_cooldown_seconds=900))
    first = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="dup", proposed_stake=1))
    second = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="dup", proposed_stake=1))
    assert first.decision == RiskDecisionType.APPROVE
    assert second.decision == RiskDecisionType.REJECT
    assert "duplicate" in second.reason


def test_exposure_rejection(tmp_path):
    engine = RiskEngine(tmp_path / "risk.db", RiskConfig(max_exposure_per_venue=5, max_exposure_per_market=5))
    engine.record_position("kalshi", "KX", 4)
    decision = engine.evaluate(RiskRequest(venue="kalshi", market="KX", opportunity_id="next", proposed_stake=2))
    assert decision.decision == RiskDecisionType.REJECT
    assert "max exposure" in decision.reason
