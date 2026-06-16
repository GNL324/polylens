from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    TraderDiscoveryCandidate,
    save_discovery_candidate,
)
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, save_wallet_report
from src.intelligence.polymarket_leaderboard_client import (
    LeaderboardEntry,
    LeaderboardFetchRequest,
    PolymarketLeaderboardClient,
)
from src.intelligence.wallet_synthetic_filter import filter_production_wallets, is_synthetic_wallet
from src.sqlite_utils import closing_connection

DEFAULT_LEADERBOARD_FETCH_PROFILES: tuple[dict[str, Any], ...] = (
    {"category": "OVERALL", "time_period": "MONTH", "order_by": "PNL", "limit": 50},
    {"category": "OVERALL", "time_period": "WEEK", "order_by": "VOL", "limit": 50},
)
SOURCE_NAME = "polymarket_leaderboard"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, "analytics_only": True, **payload}


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _rank_confidence(rank: int) -> float:
    if rank <= 0:
        return 0.55
    return round(min(0.95, 0.45 + max(0, 51 - rank) / 100.0), 4)


def _rank_discovery_score(rank: int) -> int:
    if rank <= 0:
        return 40
    return max(1, min(100, 101 - rank))


def init_polymarket_leaderboard_db(traders_db_path: str | Path = DEFAULT_TRADERS_DB) -> None:
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS polymarket_leaderboard_fetches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                status TEXT NOT NULL,
                category TEXT NOT NULL,
                time_period TEXT NOT NULL,
                order_by TEXT NOT NULL,
                fetch_limit INTEGER NOT NULL,
                fetch_offset INTEGER NOT NULL DEFAULT 0,
                entry_count INTEGER NOT NULL DEFAULT 0,
                wallet_count INTEGER NOT NULL DEFAULT 0,
                synthetic_rejected INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS polymarket_leaderboard_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fetch_id INTEGER NOT NULL,
                rank INTEGER NOT NULL DEFAULT 0,
                wallet TEXT NOT NULL,
                user_name TEXT NOT NULL DEFAULT '',
                vol REAL NOT NULL DEFAULT 0.0,
                pnl REAL NOT NULL DEFAULT 0.0,
                confidence REAL NOT NULL DEFAULT 0.0,
                discovery_score INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(fetch_id) REFERENCES polymarket_leaderboard_fetches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_polymarket_leaderboard_fetches_time
                ON polymarket_leaderboard_fetches(finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_polymarket_leaderboard_entries_fetch
                ON polymarket_leaderboard_entries(fetch_id, rank ASC);
            CREATE INDEX IF NOT EXISTS idx_polymarket_leaderboard_entries_wallet
                ON polymarket_leaderboard_entries(wallet, recorded_at DESC);
            """
        )


@dataclass(frozen=True)
class ExtractedLeaderboardWallet:
    wallet: str
    rank: int
    user_name: str
    vol: float
    pnl: float
    confidence: float
    discovery_score: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "rank": self.rank,
            "user_name": self.user_name,
            "vol": self.vol,
            "pnl": self.pnl,
            "confidence": self.confidence,
            "discovery_score": self.discovery_score,
            "metadata": self.metadata,
        }


def extract_leaderboard_wallets(
    entries: list[LeaderboardEntry],
    *,
    category: str = "OVERALL",
    time_period: str = "MONTH",
    order_by: str = "PNL",
) -> tuple[list[ExtractedLeaderboardWallet], int]:
    """Extract production-eligible wallets from leaderboard entries."""
    extracted: list[ExtractedLeaderboardWallet] = []
    synthetic_rejected = 0
    seen: set[str] = set()
    for entry in entries:
        wallet = _normalize_wallet(entry.proxy_wallet)
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        metadata = {
            "source": SOURCE_NAME,
            "category": category,
            "time_period": time_period,
            "order_by": order_by,
            "rank": entry.rank,
            "user_name": entry.user_name,
            "vol": entry.vol,
            "pnl": entry.pnl,
            **entry.metadata,
        }
        if is_synthetic_wallet(wallet, metadata=metadata):
            synthetic_rejected += 1
            continue
        extracted.append(
            ExtractedLeaderboardWallet(
                wallet=wallet,
                rank=entry.rank,
                user_name=entry.user_name,
                vol=entry.vol,
                pnl=entry.pnl,
                confidence=_rank_confidence(entry.rank),
                discovery_score=_rank_discovery_score(entry.rank),
                metadata=metadata,
            )
        )
    return extracted, synthetic_rejected


def _persist_fetch(
    *,
    traders_db_path: str | Path,
    started_at: str,
    finished_at: str,
    duration_ms: float,
    status: str,
    request: LeaderboardFetchRequest,
    entry_count: int,
    wallet_count: int,
    synthetic_rejected: int,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    with closing_connection(traders_db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO polymarket_leaderboard_fetches (
                started_at, finished_at, duration_ms, status, category, time_period, order_by,
                fetch_limit, fetch_offset, entry_count, wallet_count, synthetic_rejected, error, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                duration_ms,
                status,
                request.category,
                request.time_period,
                request.order_by,
                request.limit,
                request.offset,
                entry_count,
                wallet_count,
                synthetic_rejected,
                error,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)


def _persist_entries(
    *,
    traders_db_path: str | Path,
    fetch_id: int,
    wallets: list[ExtractedLeaderboardWallet],
) -> None:
    recorded_at = _utc_now()
    with closing_connection(traders_db_path) as conn:
        for wallet in wallets:
            conn.execute(
                """
                INSERT INTO polymarket_leaderboard_entries (
                    fetch_id, rank, wallet, user_name, vol, pnl, confidence, discovery_score,
                    metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetch_id,
                    wallet.rank,
                    wallet.wallet,
                    wallet.user_name,
                    wallet.vol,
                    wallet.pnl,
                    wallet.confidence,
                    wallet.discovery_score,
                    json.dumps(wallet.metadata, sort_keys=True),
                    recorded_at,
                ),
            )


def run_leaderboard_fetch(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    category: str = "OVERALL",
    time_period: str = "MONTH",
    order_by: str = "PNL",
    limit: int = 50,
    offset: int = 0,
    client: PolymarketLeaderboardClient | None = None,
) -> dict[str, Any]:
    init_polymarket_leaderboard_db(traders_db_path)
    request = LeaderboardFetchRequest(
        category=category,
        time_period=time_period,
        order_by=order_by,
        limit=limit,
        offset=offset,
    )
    started_at = _utc_now()
    start_ts = time.time()
    leaderboard_client = client or PolymarketLeaderboardClient()
    try:
        entries = leaderboard_client.fetch_leaderboard(
            category=category,
            time_period=time_period,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        wallets, synthetic_rejected = extract_leaderboard_wallets(
            entries,
            category=category,
            time_period=time_period,
            order_by=order_by,
        )
        finished_at = _utc_now()
        duration_ms = round((time.time() - start_ts) * 1000.0, 2)
        fetch_id = _persist_fetch(
            traders_db_path=traders_db_path,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status="success",
            request=request,
            entry_count=len(entries),
            wallet_count=len(wallets),
            synthetic_rejected=synthetic_rejected,
            metadata={"wallets": [row.wallet for row in wallets[:10]]},
        )
        _persist_entries(traders_db_path=traders_db_path, fetch_id=fetch_id, wallets=wallets)
        return _with_flags(
            {
                "fetch_id": fetch_id,
                "status": "success",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "category": category,
                "time_period": time_period,
                "order_by": order_by,
                "entry_count": len(entries),
                "wallet_count": len(wallets),
                "synthetic_rejected": synthetic_rejected,
                "wallets": [row.to_dict() for row in wallets[:25]],
            }
        )
    except Exception as exc:
        finished_at = _utc_now()
        duration_ms = round((time.time() - start_ts) * 1000.0, 2)
        fetch_id = _persist_fetch(
            traders_db_path=traders_db_path,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status="error",
            request=request,
            entry_count=0,
            wallet_count=0,
            synthetic_rejected=0,
            error=str(exc),
        )
        return _with_flags(
            {
                "fetch_id": fetch_id,
                "status": "error",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "error": str(exc),
            }
        )


def load_latest_leaderboard_wallets(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    limit: int = 100,
    successful_only: bool = True,
) -> list[ExtractedLeaderboardWallet]:
    init_polymarket_leaderboard_db(traders_db_path)
    status_clause = "AND f.status = 'success'" if successful_only else ""
    query = f"""
        SELECT e.rank, e.wallet, e.user_name, e.vol, e.pnl, e.confidence, e.discovery_score, e.metadata_json
        FROM polymarket_leaderboard_entries e
        JOIN polymarket_leaderboard_fetches f ON f.id = e.fetch_id
        WHERE 1=1 {status_clause}
        ORDER BY f.finished_at DESC, e.rank ASC, e.id DESC
        LIMIT ?
    """
    with closing_connection(traders_db_path) as conn:
        rows = conn.execute(query, (max(int(limit), 0),)).fetchall()
    wallets: list[ExtractedLeaderboardWallet] = []
    seen: set[str] = set()
    for row in rows:
        wallet = _normalize_wallet(str(row["wallet"]))
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        metadata = json.loads(row["metadata_json"] or "{}")
        wallets.append(
            ExtractedLeaderboardWallet(
                wallet=wallet,
                rank=int(row["rank"] or 0),
                user_name=str(row["user_name"] or ""),
                vol=float(row["vol"] or 0.0),
                pnl=float(row["pnl"] or 0.0),
                confidence=float(row["confidence"] or 0.0),
                discovery_score=int(row["discovery_score"] or 0),
                metadata=metadata,
            )
        )
    return wallets


def register_leaderboard_wallets_in_discovery(
    wallets: list[ExtractedLeaderboardWallet],
    *,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> dict[str, Any]:
    ingested = 0
    for wallet in wallets:
        save_discovery_candidate(
            TraderDiscoveryCandidate(
                wallet=wallet.wallet,
                source=SOURCE_NAME,
                discovery_score=wallet.discovery_score,
                evidence_count=max(1, wallet.rank or 1),
                markets_seen=[],
            ),
            db_path=discovery_db_path,
        )
        ingested += 1
    return {"ingested": ingested, "wallets": [row.wallet for row in wallets]}


def register_leaderboard_wallets_in_registry(
    wallets: list[ExtractedLeaderboardWallet],
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
) -> dict[str, Any]:
    """Seed minimal registry profiles for leaderboard wallets (analytics-only)."""
    registered = 0
    for wallet in wallets:
        save_wallet_report(
            {
                "wallet": wallet.wallet,
                "classification": "leaderboard_trader",
                "confidence": wallet.confidence,
                "metrics": {
                    "trade_count": max(1, wallet.rank or 1),
                    "markets_traded": 0,
                    "leaderboard_rank": wallet.rank,
                    "leaderboard_vol": wallet.vol,
                    "leaderboard_pnl": wallet.pnl,
                },
                "signals": [],
                "metadata": wallet.metadata,
            },
            watch_score=max(1, wallet.discovery_score),
            db_path=str(traders_db_path),
        )
        registered += 1
    return {"registered": registered, "wallets": [row.wallet for row in wallets]}


def run_leaderboard_ingestion(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    profiles: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    limit: int = 50,
    client: PolymarketLeaderboardClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    register_registry: bool = False,
) -> dict[str, Any]:
    """Fetch leaderboard profiles, persist wallets, and register in discovery."""
    init_polymarket_leaderboard_db(traders_db_path)
    fetch_profiles = list(profiles or DEFAULT_LEADERBOARD_FETCH_PROFILES)
    started_at = _utc_now()
    start_ts = time.time()
    fetch_results: list[dict[str, Any]] = []
    all_wallets: list[ExtractedLeaderboardWallet] = []
    synthetic_rejected = 0

    for profile in fetch_profiles:
        result = run_leaderboard_fetch(
            traders_db_path=traders_db_path,
            category=str(profile.get("category") or "OVERALL"),
            time_period=str(profile.get("time_period") or "MONTH"),
            order_by=str(profile.get("order_by") or "PNL"),
            limit=int(profile.get("limit") or limit),
            offset=int(profile.get("offset") or 0),
            client=client,
        )
        fetch_results.append(result)
        synthetic_rejected += int(result.get("synthetic_rejected") or 0)
        if result.get("status") == "success":
            for row in result.get("wallets") or []:
                if not isinstance(row, dict):
                    continue
                all_wallets.append(
                    ExtractedLeaderboardWallet(
                        wallet=_normalize_wallet(str(row.get("wallet") or "")),
                        rank=int(row.get("rank") or 0),
                        user_name=str(row.get("user_name") or ""),
                        vol=float(row.get("vol") or 0.0),
                        pnl=float(row.get("pnl") or 0.0),
                        confidence=float(row.get("confidence") or 0.0),
                        discovery_score=int(row.get("discovery_score") or 0),
                        metadata=dict(row.get("metadata") or {}),
                    )
                )
        if sleep:
            sleep(0.1)

    deduped: dict[str, ExtractedLeaderboardWallet] = {}
    for wallet in all_wallets:
        existing = deduped.get(wallet.wallet)
        if existing is None or wallet.discovery_score > existing.discovery_score:
            deduped[wallet.wallet] = wallet
    merged = sorted(deduped.values(), key=lambda row: (-row.discovery_score, row.wallet))
    production_wallets = [
        row
        for row in merged
        if row.wallet in set(filter_production_wallets([row.wallet]))
    ]

    discovery_result = register_leaderboard_wallets_in_discovery(
        production_wallets,
        discovery_db_path=discovery_db_path,
    )
    registry_result = (
        register_leaderboard_wallets_in_registry(production_wallets, traders_db_path=traders_db_path)
        if register_registry
        else {"registered": 0, "wallets": []}
    )

    finished_at = _utc_now()
    duration_ms = round((time.time() - start_ts) * 1000.0, 2)
    errors = [row.get("error") for row in fetch_results if row.get("status") == "error"]
    status = "success" if not errors else ("partial" if any(row.get("status") == "success" for row in fetch_results) else "error")

    return _with_flags(
        {
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "fetch_count": len(fetch_results),
            "fetch_results": fetch_results,
            "wallet_count": len(production_wallets),
            "synthetic_rejected": synthetic_rejected,
            "discovery": discovery_result,
            "registry": registry_result,
            "wallets": [row.to_dict() for row in production_wallets[:25]],
            "errors": [error for error in errors if error],
        }
    )


def load_recent_leaderboard_fetches(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    limit: int = 10,
) -> list[dict[str, Any]]:
    init_polymarket_leaderboard_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, duration_ms, status, category, time_period, order_by,
                   fetch_limit, entry_count, wallet_count, synthetic_rejected, error, metadata_json
            FROM polymarket_leaderboard_fetches
            ORDER BY finished_at DESC, id DESC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    return [
        {
            **dict(row),
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }
        for row in rows
    ]


def leaderboard_health_report(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> dict[str, Any]:
    init_polymarket_leaderboard_db(traders_db_path)
    recent = load_recent_leaderboard_fetches(traders_db_path=traders_db_path, limit=20)
    latest_success = next((row for row in recent if row.get("status") == "success"), None)
    latest_any = recent[0] if recent else None
    wallets = load_latest_leaderboard_wallets(traders_db_path=traders_db_path, limit=200)
    success_count = sum(1 for row in recent if row.get("status") == "success")
    error_count = sum(1 for row in recent if row.get("status") == "error")

    from src.analysis.trader_discovery import load_discovered_wallets

    discovery_rows = [
        row
        for row in load_discovered_wallets(db_path=discovery_db_path, limit=500)
        if row.source == SOURCE_NAME
    ]

    if latest_success is None:
        health_status = "unknown" if not recent else "unhealthy"
    elif error_count == 0:
        health_status = "healthy"
    elif success_count > 0:
        health_status = "degraded"
    else:
        health_status = "unhealthy"

    return _with_flags(
        {
            "source": SOURCE_NAME,
            "health_status": health_status,
            "latest_fetch": latest_any,
            "latest_successful_fetch": latest_success,
            "recent_fetch_count": len(recent),
            "recent_success_count": success_count,
            "recent_error_count": error_count,
            "tracked_wallet_count": len(wallets),
            "discovery_wallet_count": len(discovery_rows),
            "synthetic_rejected_total": sum(int(row.get("synthetic_rejected") or 0) for row in recent),
            "recent_fetches": recent[:5],
        }
    )


def leaderboard_status(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_limit: int = 25,
) -> dict[str, Any]:
    health = leaderboard_health_report(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    wallets = load_latest_leaderboard_wallets(traders_db_path=traders_db_path, limit=wallet_limit)
    return _with_flags(
        {
            **health,
            "top_wallets": [row.to_dict() for row in wallets[:wallet_limit]],
        }
    )
