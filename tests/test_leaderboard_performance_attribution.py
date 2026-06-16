"""Tests for leaderboard performance attribution (read-only analytics)."""
from __future__ import annotations

import json
import math

import pytest

from src.analysis.trader_discovery import load_discovered_wallets
from src.intelligence.leaderboard_performance_attribution import (
    STRATEGY_CLUSTERS,
    leaderboard_alpha_rankings,
    wallet_follow_candidates,
    wallet_performance_breakdown,
    wallet_strategy_clustering,
)
from src.intelligence.polymarket_leaderboard_ingestion import (
    SOURCE_NAME,
    run_leaderboard_fetch,
    run_leaderboard_ingestion,
)

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


def _mock_client(payload: list[dict] | None = None):
    from src.intelligence.polymarket_leaderboard_client import PolymarketLeaderboardClient

    body = payload if payload is not None else _sample_payload()

    def opener(request, timeout=30.0):
        return _MockResponse(body)

    return PolymarketLeaderboardClient(opener=opener)


@pytest.fixture
def seeded_dbs(tmp_path):
    traders_db = tmp_path / "traders.db"
    discovery_db = tmp_path / "discovery.db"
    run_leaderboard_fetch(traders_db_path=traders_db, client=_mock_client(), limit=50)
    run_leaderboard_ingestion(
        traders_db_path=traders_db,
        discovery_db_path=discovery_db,
        profiles=[{"category": "OVERALL", "time_period": "MONTH", "order_by": "PNL", "limit": 50}],
        client=_mock_client(),
        sleep=lambda _: None,
    )
    return {"traders_db": traders_db, "discovery_db": discovery_db}


def test_leaderboard_alpha_rankings_leaderboard_only(seeded_dbs):
    result = leaderboard_alpha_rankings(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
        limit=10,
    )
    assert result["leaderboard_only"] is True
    assert result["real_wallet_only"] is True
    assert result["read_only"] is True
    assert result["analytics_only"] is True
    assert result["synthetic_wallet_count"] == 0
    assert SYNTHETIC_WALLET not in {row["wallet"] for row in result["rankings"]}
    for row in result["rankings"]:
        assert "alpha_score" in row
        assert "alpha_confidence" in row
        assert "alpha_rank" in row


def test_leaderboard_alpha_rankings_empty_is_safe(tmp_path):
    result = leaderboard_alpha_rankings(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
    )
    assert result["rankings"] == []
    assert result["rankings_count"] == 0
    assert result["synthetic_wallet_count"] == 0


def test_wallet_performance_breakdown_shape(seeded_dbs):
    result = wallet_performance_breakdown(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
    )
    assert result["source"] == SOURCE_NAME
    assert result["read_only"] is True
    assert result["analytics_only"] is True
    assert result["synthetic_wallet_count"] == 0
    assert "total_wallets" in result
    assert "accepted_wallets" in result
    assert "probation_wallets" in result
    assert "rejected_wallets" in result
    assert "average_alpha_score" in result
    assert "median_alpha_score" in result
    assert "top_wallet" in result
    assert "top_alpha_score" in result


def test_wallet_performance_breakdown_excludes_synthetic(seeded_dbs):
    result = wallet_performance_breakdown(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
    )
    all_mentioned = (
        result.get("accepted", [])
        + result.get("probation", [])
        + result.get("rejected", [])
    )
    assert SYNTHETIC_WALLET not in all_mentioned


def test_wallet_follow_candidates_shape(seeded_dbs):
    result = wallet_follow_candidates(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
        limit=5,
    )
    assert result["source"] == SOURCE_NAME
    assert result["read_only"] is True
    assert result["count"] >= 0
    assert len(result["candidates"]) <= 5
    for row in result["candidates"]:
        assert "wallet" in row
        assert "alpha_score" in row
        assert "confidence" in row
        assert "discovery_score" in row
        assert "registry_status" in row
        assert "reason" in row
        assert not is_synthetic_wallet(row["wallet"])


def test_wallet_follow_candidates_empty_is_safe(tmp_path):
    result = wallet_follow_candidates(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
    )
    assert result["candidates"] == []
    assert result["count"] == 0


def test_wallet_strategy_clustering_shape(seeded_dbs):
    result = wallet_strategy_clustering(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
        leaderboard_only=True,
    )
    assert result["leaderboard_only"] is True
    assert result["read_only"] is True
    assert result["synthetic_wallet_count"] == 0
    categories = {row["category"] for row in result["clusters"]}
    assert categories.issubset(set(STRATEGY_CLUSTERS))
    for row in result["clusters"]:
        assert "wallet_count" in row
        assert "average_alpha_score" in row
        assert "top_wallets" in row
        assert "confidence_notes" in row


def test_wallet_strategy_clustering_empty_is_safe(tmp_path):
    result = wallet_strategy_clustering(
        traders_db_path=tmp_path / "traders.db",
        discovery_db_path=tmp_path / "discovery.db",
        leaderboard_only=True,
    )
    assert result["clusters"] == []
    assert result["total_classified"] == 0


def test_attribution_commands_do_not_mutate_dbs(seeded_dbs):
    before_wallets = load_discovered_wallets(db_path=seeded_dbs["discovery_db"], limit=100)
    leaderboard_alpha_rankings(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
    )
    wallet_performance_breakdown(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
    )
    wallet_follow_candidates(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
    )
    wallet_strategy_clustering(
        traders_db_path=seeded_dbs["traders_db"],
        discovery_db_path=seeded_dbs["discovery_db"],
        leaderboard_only=True,
    )
    after_wallets = load_discovered_wallets(db_path=seeded_dbs["discovery_db"], limit=100)
    assert len(after_wallets) == len(before_wallets)
    assert {w.wallet for w in before_wallets} == {w.wallet for w in after_wallets}


def test_classify_leaderboard_wallet_unknown_when_no_metadata():
    from src.intelligence.leaderboard_performance_attribution import _classify_leaderboard_wallet

    assert _classify_leaderboard_wallet("0x1234", {}) == "unknown"


def test_classify_leaderboard_wallet_crypto_specialist():
    from src.intelligence.leaderboard_performance_attribution import _classify_leaderboard_wallet

    assert _classify_leaderboard_wallet("0x1234", {"category": "CRYPTO"}) == "crypto_specialist"


def test_classify_leaderboard_wallet_sports_specialist():
    from src.intelligence.leaderboard_performance_attribution import _classify_leaderboard_wallet

    assert _classify_leaderboard_wallet("0x1234", {"category": "SPORTS", "rank": 3, "pnl": 100, "vol": 1_000_000}) == "sports_specialist"


def test_classify_leaderboard_wallet_market_maker():
    from src.intelligence.leaderboard_performance_attribution import _classify_leaderboard_wallet

    assert _classify_leaderboard_wallet("0x1234", {"vol": 20_000_000, "pnl": 100}) == "market_maker"


def test_leaderboard_alpha_rankings_cli_flag_registers():
    import subprocess

    result = subprocess.run(
        [".venv/bin/python", "-m", "src.cli", "wallet-alpha-rankings", "--leaderboard-only", "--json"],
        cwd="/home/noel/polylens",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["leaderboard_only"] is True
    assert payload["read_only"] is True
    assert "rankings" in payload


def test_wallet_performance_breakdown_cli():
    import subprocess

    result = subprocess.run(
        [".venv/bin/python", "-m", "src.cli", "wallet-performance-breakdown", "--json"],
        cwd="/home/noel/polylens",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["analytics_only"] is True
    assert "total_wallets" in payload


def test_wallet_follow_candidates_cli():
    import subprocess

    result = subprocess.run(
        [".venv/bin/python", "-m", "src.cli", "wallet-follow-candidates", "--json"],
        cwd="/home/noel/polylens",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "candidates" in payload


def test_wallet_strategy_clustering_cli():
    import subprocess

    result = subprocess.run(
        [".venv/bin/python", "-m", "src.cli", "wallet-strategy-clustering", "--leaderboard-only", "--json"],
        cwd="/home/noel/polylens",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["leaderboard_only"] is True
    assert "clusters" in payload


def is_synthetic_wallet(wallet: str) -> bool:
    from src.intelligence.wallet_synthetic_filter import is_synthetic_wallet as _is_synthetic

    return _is_synthetic(wallet)
