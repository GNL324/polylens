from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    TraderDiscoveryCandidate,
    discover_from_registry,
    discover_trader_candidates,
    load_discovered_wallets,
    save_discovery_candidate,
)
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_scanner import DEFAULT_WATCHLIST_PATH, DEFAULT_WALLET_EXPORT_DIR, discover_wallets, scan_wallets
from src.intelligence.strategy_classifier import StrategyClassifier
from src.intelligence.wallet_scoring import WalletScore, WalletScorer
from src.intelligence.wallet_tracker import WalletTracker
from src.sqlite_utils import closing_connection

LIFECYCLE_STATES = ("active", "watchlist", "probation", "retired")
DEFAULT_DISCOVERY_LIMIT = 100
DEFAULT_RATE_LIMIT_SECONDS = 0.25
DEFAULT_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class DiscoverySourceConfig:
    name: str
    enabled: bool = True
    limit: int = 50


@dataclass
class WalletDiscoveryConfig:
    sources: list[DiscoverySourceConfig] = field(
        default_factory=lambda: [
            DiscoverySourceConfig(name="registry", enabled=True, limit=50),
            DiscoverySourceConfig(name="discovery_db", enabled=True, limit=100),
            DiscoverySourceConfig(name="watchlist", enabled=True, limit=50),
            DiscoverySourceConfig(name="seed_wallet", enabled=False, limit=25),
        ]
    )
    seed_wallet: str = ""
    discovery_limit: int = DEFAULT_DISCOVERY_LIMIT
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    scan_discovered: bool = False
    classify_discovered: bool = True


class DiscoverySource(Protocol):
    name: str

    def discover(self, *, limit: int) -> list[TraderDiscoveryCandidate]:
        ...


@dataclass
class RegistryDiscoverySource:
    traders_db_path: str | Path = DEFAULT_TRADERS_DB
    name: str = "registry"

    def discover(self, *, limit: int) -> list[TraderDiscoveryCandidate]:
        wallets = discover_from_registry(db_path=self.traders_db_path, limit=limit)
        return [
            TraderDiscoveryCandidate(
                wallet=wallet,
                source=self.name,
                discovery_score=max(1, 50 - index),
                evidence_count=1,
                markets_seen=[],
            )
            for index, wallet in enumerate(wallets)
        ]


@dataclass
class DiscoveryDbSource:
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB
    name: str = "discovery_db"

    def discover(self, *, limit: int) -> list[TraderDiscoveryCandidate]:
        return load_discovered_wallets(db_path=self.discovery_db_path, limit=limit)


@dataclass
class WatchlistDiscoverySource:
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB
    name: str = "watchlist"

    def discover(self, *, limit: int) -> list[TraderDiscoveryCandidate]:
        wallets = discover_wallets(watchlist=self.watchlist_path, limit=limit)
        return [
            TraderDiscoveryCandidate(wallet=wallet, source=self.name, discovery_score=35, evidence_count=1, markets_seen=[])
            for wallet in wallets[:limit]
        ]


@dataclass
class SeedWalletDiscoverySource:
    seed_wallet: str
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH
    traders_db_path: str | Path = DEFAULT_TRADERS_DB
    name: str = "seed_wallet"

    def discover(self, *, limit: int) -> list[TraderDiscoveryCandidate]:
        if not self.seed_wallet:
            return []
        return discover_trader_candidates(
            wallet=self.seed_wallet,
            watchlist=self.watchlist_path,
            registry_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
            limit=limit,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _dedupe_candidates(candidates: list[TraderDiscoveryCandidate]) -> list[TraderDiscoveryCandidate]:
    by_wallet: dict[str, TraderDiscoveryCandidate] = {}
    for candidate in candidates:
        wallet = _normalize_wallet(candidate.wallet)
        if not wallet:
            continue
        existing = by_wallet.get(wallet)
        if existing is None or candidate.discovery_score > existing.discovery_score:
            by_wallet[wallet] = TraderDiscoveryCandidate(
                wallet=wallet,
                source=candidate.source,
                discovery_score=int(candidate.discovery_score),
                evidence_count=int(candidate.evidence_count),
                markets_seen=list(candidate.markets_seen),
            )
    return sorted(by_wallet.values(), key=lambda row: (-row.discovery_score, row.wallet))


def init_wallet_discovery_db(
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> None:
    from src.analysis.trader_discovery import _init_discovery_db

    _init_discovery_db(discovery_db_path)
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_lifecycle (
                wallet TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'probation',
                score REAL NOT NULL DEFAULT 0.0,
                confidence REAL NOT NULL DEFAULT 0.0,
                category TEXT NOT NULL DEFAULT 'unscored',
                rank INTEGER NOT NULL DEFAULT 0,
                last_scored_at TEXT,
                last_active_at TEXT,
                stale_days INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_score_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                score REAL NOT NULL,
                confidence REAL NOT NULL,
                category TEXT NOT NULL,
                rank INTEGER NOT NULL,
                components_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                previous_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                score REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_lifecycle_state ON wallet_lifecycle(state);
            CREATE INDEX IF NOT EXISTS idx_wallet_score_history_wallet ON wallet_score_history(wallet, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_lifecycle_events_wallet ON wallet_lifecycle_events(wallet, recorded_at DESC);
            """
        )
    with closing_connection(discovery_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type TEXT NOT NULL,
                seed_wallet TEXT,
                wallets_found INTEGER NOT NULL DEFAULT 0,
                new_wallets INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                run_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_runs_time ON discovery_runs(run_at DESC);
            """
        )


class WalletDiscoveryEngine:
    """Autonomous wallet discovery orchestrating existing Polylens discovery and scoring systems."""

    def __init__(
        self,
        *,
        config: WalletDiscoveryConfig | None = None,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
        watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or WalletDiscoveryConfig()
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.wallet_export_dir = Path(wallet_export_dir)
        self.watchlist_path = Path(watchlist_path)
        self._sleep = sleep
        self._cache: dict[str, tuple[float, list[TraderDiscoveryCandidate]]] = {}
        self._tracker = WalletTracker(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
            wallet_export_dir=self.wallet_export_dir,
            watchlist_path=self.watchlist_path,
        )
        self._scorer = WalletScorer(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
        )
        self._classifier = StrategyClassifier(traders_db_path=self.traders_db_path)
        init_wallet_discovery_db(self.traders_db_path, self.discovery_db_path)

    def _build_sources(self) -> list[DiscoverySource]:
        sources: list[DiscoverySource] = []
        for source_cfg in self.config.sources:
            if not source_cfg.enabled:
                continue
            if source_cfg.name == "registry":
                sources.append(RegistryDiscoverySource(traders_db_path=self.traders_db_path))
            elif source_cfg.name == "discovery_db":
                sources.append(DiscoveryDbSource(discovery_db_path=self.discovery_db_path))
            elif source_cfg.name == "watchlist":
                sources.append(WatchlistDiscoverySource(watchlist_path=self.watchlist_path, discovery_db_path=self.discovery_db_path))
            elif source_cfg.name == "seed_wallet" and self.config.seed_wallet:
                sources.append(
                    SeedWalletDiscoverySource(
                        seed_wallet=self.config.seed_wallet,
                        discovery_db_path=self.discovery_db_path,
                        watchlist_path=self.watchlist_path,
                        traders_db_path=self.traders_db_path,
                    )
                )
        return sources

    def _cache_get(self, key: str) -> list[TraderDiscoveryCandidate] | None:
        cached = self._cache.get(key)
        if cached is None:
            return None
        ts, candidates = cached
        if time.time() - ts > self.config.cache_ttl_seconds:
            return None
        return candidates

    def _cache_set(self, key: str, candidates: list[TraderDiscoveryCandidate]) -> None:
        self._cache[key] = (time.time(), candidates)

    def discover_new_wallets(self, *, limit: int | None = None) -> dict[str, Any]:
        limit = limit or self.config.discovery_limit
        all_candidates: list[TraderDiscoveryCandidate] = []
        source_counts: dict[str, int] = {}

        for source in self._build_sources():
            cache_key = f"{source.name}:{limit}"
            cached = self._cache_get(cache_key)
            if cached is not None:
                candidates = cached
            else:
                candidates = source.discover(limit=limit)
                self._cache_set(cache_key, candidates)
                if self.config.rate_limit_seconds > 0:
                    self._sleep(self.config.rate_limit_seconds)
            source_counts[source.name] = len(candidates)
            all_candidates.extend(candidates)

        merged = _dedupe_candidates(all_candidates)[:limit]
        new_wallets = 0
        existing = {row.wallet for row in load_discovered_wallets(db_path=self.discovery_db_path, limit=10_000)}
        for candidate in merged:
            if candidate.wallet not in existing:
                new_wallets += 1
            save_discovery_candidate(candidate, db_path=self.discovery_db_path)
        self._record_discovery_run(
            run_type="aggregate",
            seed_wallet=self.config.seed_wallet or None,
            wallets_found=len(merged),
            new_wallets=new_wallets,
            metadata={"source_counts": source_counts},
        )

        if self.config.scan_discovered and merged:
            scan_wallets(
                wallets=[candidate.wallet for candidate in merged],
                limit=limit,
                traders_db_path=self.traders_db_path,
                wallet_export_dir=self.wallet_export_dir,
            )

        return {
            "candidates_found": len(merged),
            "new_wallets": new_wallets,
            "source_counts": source_counts,
            "wallets": [candidate.wallet for candidate in merged],
            "candidates": [candidate.to_dict() for candidate in merged],
        }

    def ingest_candidates(self, candidates: list[TraderDiscoveryCandidate]) -> dict[str, Any]:
        merged = _dedupe_candidates(candidates)
        for candidate in merged:
            save_discovery_candidate(candidate, db_path=self.discovery_db_path)
        return {"ingested": len(merged), "wallets": [candidate.wallet for candidate in merged]}

    def incremental_refresh(self, wallets: list[str] | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
        targets = wallets or [candidate.wallet for candidate in load_discovered_wallets(db_path=self.discovery_db_path, limit=self.config.discovery_limit)]
        return self._tracker.refresh_wallets(targets, limit=limit)

    def score_and_rank(self, wallets: list[str] | None = None) -> list[WalletScore]:
        if wallets is None:
            discovered = [candidate.wallet for candidate in load_discovered_wallets(db_path=self.discovery_db_path, limit=self.config.discovery_limit)]
            registry = [row.wallet for row in list_traders(limit=self.config.discovery_limit, db_path=str(self.traders_db_path))]
            seen: set[str] = set()
            wallets = []
            for wallet in discovered + registry:
                normalized = _normalize_wallet(wallet)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    wallets.append(normalized)
        ranked = self._scorer.rank_wallets(wallets)
        now = _utc_now()
        with closing_connection(self.traders_db_path) as conn:
            for row in ranked:
                conn.execute(
                    """
                    INSERT INTO wallet_score_history (
                        wallet, score, confidence, category, rank, components_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.wallet,
                        row.score,
                        row.confidence,
                        row.category,
                        row.rank,
                        json.dumps(row.components, sort_keys=True),
                        now,
                    ),
                )
        return ranked

    def evaluate_lifecycle(self, wallet_score: WalletScore, *, stale_days: int = 0) -> str:
        score = wallet_score.score
        if stale_days >= 90 or score < 20:
            return "retired"
        if stale_days >= 45 and score < 45:
            return "retired"
        if score >= 70 and wallet_score.confidence >= 0.4:
            return "active"
        if score >= 50:
            return "watchlist"
        if score >= 30 or stale_days < 30:
            return "probation"
        return "retired"

    def apply_lifecycle(self, ranked: list[WalletScore]) -> dict[str, Any]:
        now = _utc_now()
        promotions = 0
        demotions = 0
        state_counts = {state: 0 for state in LIFECYCLE_STATES}

        with closing_connection(self.traders_db_path) as conn:
            for row in ranked:
                latest_ts = None
                activity_path = self.wallet_export_dir / f"{row.wallet}_activity.json"
                if activity_path.exists():
                    payload = json.loads(activity_path.read_text(encoding="utf-8"))
                    events = payload.get("events", [])
                    if events:
                        latest_ts = max(float(event.get("timestamp") or 0.0) for event in events if isinstance(event, dict))
                stale_days = 0
                if latest_ts:
                    stale_days = int(max(0.0, (datetime.now(timezone.utc).timestamp() - latest_ts) / 86_400.0))

                new_state = self.evaluate_lifecycle(row, stale_days=stale_days)
                existing = conn.execute(
                    "SELECT state, score FROM wallet_lifecycle WHERE wallet = ?",
                    (row.wallet,),
                ).fetchone()
                previous_state = str(existing["state"]) if existing else "probation"
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO wallet_lifecycle (
                            wallet, state, score, confidence, category, rank,
                            last_scored_at, last_active_at, stale_days, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row.wallet,
                            new_state,
                            row.score,
                            row.confidence,
                            row.category,
                            row.rank,
                            now,
                            _utc_now() if latest_ts else None,
                            stale_days,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE wallet_lifecycle
                        SET state = ?, score = ?, confidence = ?, category = ?, rank = ?,
                            last_scored_at = ?, last_active_at = ?, stale_days = ?, updated_at = ?
                        WHERE wallet = ?
                        """,
                        (
                            new_state,
                            row.score,
                            row.confidence,
                            row.category,
                            row.rank,
                            now,
                            datetime.fromtimestamp(latest_ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if latest_ts else None,
                            stale_days,
                            now,
                            row.wallet,
                        ),
                    )

                if new_state != previous_state:
                    conn.execute(
                        """
                        INSERT INTO wallet_lifecycle_events (
                            wallet, previous_state, new_state, reason, score, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row.wallet,
                            previous_state,
                            new_state,
                            f"score={row.score:.2f} stale_days={stale_days}",
                            row.score,
                            now,
                        ),
                    )
                    previous_index = LIFECYCLE_STATES.index(previous_state) if previous_state in LIFECYCLE_STATES else 2
                    new_index = LIFECYCLE_STATES.index(new_state)
                    if new_index < previous_index:
                        promotions += 1
                    elif new_index > previous_index:
                        demotions += 1
                state_counts[new_state] = state_counts.get(new_state, 0) + 1

        return {"promotions": promotions, "demotions": demotions, "state_counts": state_counts}

    def run_discovery_cycle(self, *, limit: int | None = None) -> dict[str, Any]:
        discovery = self.discover_new_wallets(limit=limit)
        refresh = self.incremental_refresh(discovery.get("wallets"))
        ranked = self.score_and_rank(discovery.get("wallets"))
        lifecycle = self.apply_lifecycle(ranked)

        if self.config.classify_discovered and discovery.get("wallets"):
            profiles = []
            for wallet in discovery.get("wallets", []):
                try:
                    profiles.append(
                        self._classifier.classify_wallet(
                            wallet,
                            activity_path=self.wallet_export_dir / f"{wallet}_activity.json",
                            scan_if_missing=False,
                        )
                    )
                except (ValueError, OSError):
                    continue
            if profiles:
                self._classifier.persist_profiles(profiles)
        else:
            profiles = []

        watchlist_entries = self._tracker.build_ranked_watchlist(limit=min(50, limit or self.config.discovery_limit))
        watchlist = self._tracker.persist_watchlist(watchlist_entries)

        return {
            "read_only": True,
            "paper_only": True,
            "discovery": discovery,
            "refresh": refresh,
            "ranked_count": len(ranked),
            "top_wallets": [row.to_dict() for row in ranked[:10]],
            "lifecycle": lifecycle,
            "profiles_classified": len(profiles),
            "watchlist": watchlist,
        }

    def load_lifecycle(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        init_wallet_discovery_db(self.traders_db_path, self.discovery_db_path)
        query = """
            SELECT wallet, state, score, confidence, category, rank, last_scored_at, stale_days, updated_at
            FROM wallet_lifecycle
        """
        params: list[Any] = []
        if state:
            query += " WHERE state = ?"
            params.append(state)
        query += " ORDER BY rank ASC, score DESC LIMIT ?"
        params.append(max(int(limit), 0))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def load_recent_discoveries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with closing_connection(self.discovery_db_path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, discovery_score, discovery_source, evidence_count, first_seen, last_seen
                FROM discovered_wallets
                ORDER BY last_seen DESC, discovery_score DESC
                LIMIT ?
                """,
                (max(int(limit), 0),),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_promotion_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, previous_state, new_state, reason, score, recorded_at
                FROM wallet_lifecycle_events
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (max(int(limit), 0),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_discovery_run(
        self,
        *,
        run_type: str,
        seed_wallet: str | None,
        wallets_found: int,
        new_wallets: int,
        metadata: dict[str, Any],
    ) -> None:
        with closing_connection(self.discovery_db_path) as conn:
            conn.execute(
                """
                INSERT INTO discovery_runs (run_type, seed_wallet, wallets_found, new_wallets, metadata_json, run_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_type,
                    seed_wallet,
                    int(wallets_found),
                    int(new_wallets),
                    json.dumps(metadata, sort_keys=True),
                    _utc_now(),
                ),
            )
