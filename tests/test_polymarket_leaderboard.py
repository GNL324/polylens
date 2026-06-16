from __future__ import annotations

import json

from src.analysis.trader_discovery import load_discovered_wallets
from src.intelligence.polymarket_leaderboard_client import (
    PolymarketLeaderboardClient,
    parse_leaderboard_entry,
    parse_leaderboard_payload,
)
from src.intelligence.polymarket_leaderboard_ingestion import (
    SOURCE_NAME,
    extract_leaderboard_wallets,
    init_polymarket_leaderboard_db,
    leaderboard_health_report,
    load_latest_leaderboard_wallets,
    run_leaderboard_fetch,
    run_leaderboard_ingestion,
)
from src.intelligence.wallet_autonomy_service import CYCLE_NAMES, WalletAutonomyService
from src.intelligence.wallet_data_acquisition import (
    AcquisitionSourceConfig,
    WalletDataAcquisitionEngine,
    init_wallet_acquisition_db,
)
from src.intelligence.wallet_discovery import LeaderboardDiscoverySource, WalletDiscoveryEngine

REAL_WALLET = "0xed64a7bf029040aa331abc87902434d815ef217d"
REAL_WALLET_2 = "0xf8831548531d56ad6a4331493243c447a827cd1f"
SYNTHETIC_WALLET = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _sample_payload() -> list[dict]:
    return [
        {
            "rank": "1",
            "proxyWallet": REAL_WALLET,
            "userName": "fishalive",
            "vol": 1000.0,
            "pnl": 500.0,
        },
        {
            "rank": "2",
            "proxyWallet": REAL_WALLET_2,
            "userName": "trader2",
            "vol": 800.0,
            "pnl": 300.0,
        },
        {
            "rank": "3",
            "proxyWallet": SYNTHETIC_WALLET,
            "userName": "fixture",
            "vol": 1.0,
            "pnl": 1.0,
        },
    ]


class _MockResponse:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _mock_client(payload: list[dict] | None = None) -> PolymarketLeaderboardClient:
    body = payload if payload is not None else _sample_payload()

    def opener(request, timeout=30.0):
        return _MockResponse(body)

    return PolymarketLeaderboardClient(opener=opener)


def test_parse_leaderboard_payload_extracts_wallets():
    entries = parse_leaderboard_payload(_sample_payload())
    assert len(entries) == 3
    assert entries[0].proxy_wallet == REAL_WALLET
    assert entries[0].rank == 1


def test_parse_leaderboard_entry_rejects_invalid_wallet():
    assert parse_leaderboard_entry({"rank": "1", "proxyWallet": "not-a-wallet"}) is None


def test_extract_leaderboard_wallets_rejects_synthetic():
    entries = parse_leaderboard_payload(_sample_payload())
    wallets, synthetic_rejected = extract_leaderboard_wallets(entries)
    assert synthetic_rejected == 1
    assert len(wallets) == 2
    assert all(row.wallet != SYNTHETIC_WALLET for row in wallets)


def test_run_leaderboard_fetch_persists_entries(tmp_path):
    traders_db = tmp_path / "traders.db"
    client = _mock_client()
    result = run_leaderboard_fetch(traders_db_path=traders_db, client=client, limit=50)
    assert result["status"] == "success"
    assert result["wallet_count"] == 2
    assert result["synthetic_rejected"] == 1
    wallets = load_latest_leaderboard_wallets(traders_db_path=traders_db, limit=10)
    assert len(wallets) == 2


def test_run_leaderboard_ingestion_registers_discovery(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    client = _mock_client()
    result = run_leaderboard_ingestion(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        profiles=[{"category": "OVERALL", "time_period": "MONTH", "order_by": "PNL", "limit": 50}],
        client=client,
        sleep=lambda _: None,
    )
    assert result["status"] == "success"
    assert result["wallet_count"] >= 2
    discovery_rows = load_discovered_wallets(db_path=discovery_db, limit=50)
    assert any(row.source == SOURCE_NAME for row in discovery_rows)
    assert SYNTHETIC_WALLET not in {row.wallet for row in discovery_rows}


def test_leaderboard_acquisition_source_reads_persisted_wallets(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_acquisition_db(traders_db, discovery_db)
    run_leaderboard_fetch(traders_db_path=traders_db, client=_mock_client(), limit=50)
    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        sources=[AcquisitionSourceConfig(name="polymarket_leaderboard", limit=10)],
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    candidates = engine.discover_candidates(limit=10)
    assert any(candidate.source == SOURCE_NAME for candidate in candidates)
    assert SYNTHETIC_WALLET not in {candidate.wallet for candidate in candidates}


def test_leaderboard_discovery_source(tmp_path):
    traders_db = tmp_path / "traders.db"
    run_leaderboard_fetch(traders_db_path=traders_db, client=_mock_client(), limit=50)
    source = LeaderboardDiscoverySource(traders_db_path=traders_db)
    candidates = source.discover(limit=10)
    assert len(candidates) == 2
    assert candidates[0].source == SOURCE_NAME


def test_leaderboard_wallets_enter_acquisition_pipeline(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    init_wallet_acquisition_db(traders_db, discovery_db)
    run_leaderboard_ingestion(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        profiles=[{"category": "OVERALL", "time_period": "MONTH", "order_by": "PNL", "limit": 50}],
        client=_mock_client(),
        sleep=lambda _: None,
    )
    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        sources=[AcquisitionSourceConfig(name="polymarket_leaderboard", limit=10)],
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    report = engine.run_acquisition(limit=10)
    assert report["discovered"] >= 2
    assert report["synthetic_rejected"] >= 0
    accepted_wallets = {
        row.get("wallet")
        for row in report.get("results", [])
        if row.get("status") == "accepted"
    }
    assert REAL_WALLET in accepted_wallets or REAL_WALLET_2 in accepted_wallets


def test_leaderboard_health_report(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    run_leaderboard_fetch(traders_db_path=traders_db, client=_mock_client(), limit=50)
    health = leaderboard_health_report(traders_db_path=traders_db, discovery_db_path=discovery_db)
    assert health["read_only"] is True
    assert health["health_status"] in {"healthy", "degraded", "unknown"}
    assert health["tracked_wallet_count"] == 2


def test_autonomy_leaderboard_ingestion_cycle(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    service = WalletAutonomyService(traders_db_path=traders_db, discovery_db_path=discovery_db)
    assert "leaderboard_ingestion" in CYCLE_NAMES

    from src.intelligence import polymarket_leaderboard_ingestion as ingestion_module

    original = ingestion_module.run_leaderboard_ingestion

    def _stub(**kwargs):
        return {
            "read_only": True,
            "paper_only": True,
            "status": "success",
            "wallet_count": 2,
            "synthetic_rejected": 1,
            "discovery": {"ingested": 2},
        }

    ingestion_module.run_leaderboard_ingestion = _stub
    try:
        result = service.run_leaderboard_ingestion_cycle()
    finally:
        ingestion_module.run_leaderboard_ingestion = original

    assert result["status"] == "success"
    states = service.load_cycle_state("leaderboard_ingestion")
    assert states[0]["last_status"] == "success"


def test_wallet_discovery_engine_includes_leaderboard_source(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    run_leaderboard_fetch(traders_db_path=traders_db, client=_mock_client(), limit=50)
    engine = WalletDiscoveryEngine(traders_db_path=traders_db, discovery_db_path=discovery_db)
    payload = engine.discover_new_wallets(limit=10)
    assert payload["candidates_found"] >= 2
    assert "polymarket_leaderboard" in payload["source_counts"]


def test_init_polymarket_leaderboard_db_idempotent(tmp_path):
    traders_db = tmp_path / "traders.db"
    init_polymarket_leaderboard_db(traders_db)
    init_polymarket_leaderboard_db(traders_db)
