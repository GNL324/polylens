from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.analysis.trader_discovery import TraderDiscoveryCandidate, save_discovery_candidate
from src.analysis.trader_registry import save_wallet_report
from src.intelligence.wallet_autonomy_service import (
    CYCLE_NAMES,
    DEFAULT_CYCLE_INTERVALS_SECONDS,
    WalletAutonomyService,
)
from src.intelligence.wallet_service_health import wallet_service_health_summary

WALLET = "0x" + "b" * 40


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _wallet_report() -> dict:
    return {
        "wallet": WALLET,
        "classification": "arbitrage_trader",
        "confidence": 0.9,
        "watch_score": 88,
        "metrics": {
            "trade_count": 120,
            "markets_traded": 42,
            "overlap_ratio": 0.4,
            "merge_count": 10,
            "redeem_count": 5,
            "buy_volume": 3000,
            "sell_volume": 900,
            "btc_volume": 1500,
            "eth_volume": 800,
            "sol_volume": 700,
        },
        "signals": [],
    }


def _service(tmp_path) -> tuple[WalletAutonomyService, object, object]:
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    return service, traders_db, discovery_db


def _record_cycle(
    service: WalletAutonomyService,
    *,
    cycle_name: str,
    finished_at: datetime,
    status: str = "success",
    error: str | None = None,
    result: dict | None = None,
) -> None:
    finished_at_str = _iso(finished_at)
    service._persist_cycle_run(
        cycle_name=cycle_name,
        started_at=finished_at_str,
        finished_at=finished_at_str,
        duration_ms=10.0,
        status=status,
        error=error,
        result=result or {"ok": status == "success"},
        health_status="healthy" if status == "success" else "unhealthy",
    )


def _record_all_successful(service: WalletAutonomyService, *, finished_at: datetime) -> None:
    for cycle_name in CYCLE_NAMES:
        _record_cycle(service, cycle_name=cycle_name, finished_at=finished_at)


def test_successful_autonomy_cycles_are_healthy(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, _ = _service(tmp_path)
    _record_all_successful(service, finished_at=now)

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "healthy"
    assert health["stale_cycles"] == []
    assert health["failures"] == []
    assert health["success_rate"] == 1.0


def test_stale_cycle_detection_after_double_interval(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, _ = _service(tmp_path)
    _record_all_successful(service, finished_at=now)
    stale_at = now - timedelta(seconds=DEFAULT_CYCLE_INTERVALS_SECONDS["acquisition"] * 2 + 1)
    _record_cycle(service, cycle_name="acquisition", finished_at=stale_at)

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "degraded"
    assert health["stale_cycles"] == ["acquisition"]
    acquisition = next(row for row in health["cycles"] if row["cycle"] == "acquisition")
    assert acquisition["stale"] is True


def test_timer_edge_not_stale_at_exact_double_interval(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, _ = _service(tmp_path)
    _record_all_successful(service, finished_at=now)
    boundary = now - timedelta(seconds=DEFAULT_CYCLE_INTERVALS_SECONDS["signals"] * 2)
    _record_cycle(service, cycle_name="signals", finished_at=boundary)

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "healthy"
    assert "signals" not in health["stale_cycles"]
    signals = next(row for row in health["cycles"] if row["cycle"] == "signals")
    assert signals["stale"] is False


def test_bootstrap_dormant_success_is_not_stale_when_ecosystem_populated(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, discovery_db = _service(tmp_path)
    save_wallet_report(_wallet_report(), db_path=str(traders_db))
    save_discovery_candidate(
        TraderDiscoveryCandidate(
            wallet=WALLET,
            source="test",
            discovery_score=90,
            evidence_count=1,
            markets_seen=["market"],
        ),
        db_path=discovery_db,
    )
    _record_all_successful(service, finished_at=now)
    old_bootstrap = now - timedelta(days=10)
    _record_cycle(
        service,
        cycle_name="bootstrap",
        finished_at=old_bootstrap,
        result={"skipped": True, "reason": "ecosystem not empty"},
    )

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "healthy"
    assert "bootstrap" not in health["stale_cycles"]
    bootstrap = next(row for row in health["cycles"] if row["cycle"] == "bootstrap")
    assert bootstrap["stale"] is False


def test_empty_ecosystem_keeps_old_bootstrap_stale(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, _ = _service(tmp_path)
    _record_all_successful(service, finished_at=now)
    old_bootstrap = now - timedelta(days=10)
    _record_cycle(
        service,
        cycle_name="bootstrap",
        finished_at=old_bootstrap,
        result={"skipped": True, "reason": "ecosystem not empty"},
    )

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "degraded"
    assert "bootstrap" in health["stale_cycles"]


def test_unhealthy_when_all_cycles_are_stale(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, _ = _service(tmp_path)
    old = now - timedelta(days=30)
    _record_all_successful(service, finished_at=old)

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "unhealthy"
    assert set(health["stale_cycles"]) == set(CYCLE_NAMES)


def test_unhealthy_cycle_error_degrades_health(tmp_path):
    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    service, traders_db, _ = _service(tmp_path)
    _record_all_successful(service, finished_at=now)
    _record_cycle(
        service,
        cycle_name="discovery",
        finished_at=now,
        status="error",
        error="database is locked",
        result={"error": "database is locked"},
    )

    health = wallet_service_health_summary(
        traders_db_path=str(traders_db),
        service=service,
        now=now,
    )

    assert health["status"] == "degraded"
    assert health["stale_cycles"] == []
    assert {"cycle": "discovery", "error": "database is locked"} in health["failures"]
