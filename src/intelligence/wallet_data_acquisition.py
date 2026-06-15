from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    TraderDiscoveryCandidate,
    load_discovered_wallets,
    save_discovery_candidate,
)
from src.analysis.trader_registry import (
    DEFAULT_TRADERS_DB,
    calculate_watch_score,
    list_traders,
    load_wallet_report,
    save_wallet_report,
)
from src.analysis.trader_scanner import DEFAULT_WATCHLIST_PATH, discover_wallets
from src.analysis.wallet_activity import validate_wallet
from src.intelligence.wallet_discovery import WalletDiscoveryEngine, init_wallet_discovery_db
from src.intelligence.wallet_tracker import WalletTracker
from src.sqlite_utils import closing_connection

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DEFAULT_RATE_LIMIT_SECONDS = 0.25
DEFAULT_CACHE_TTL_SECONDS = 300.0
DEFAULT_MAX_RETRIES = 2
ACQUISITION_STATUSES = ("accepted", "rejected", "probation")


@dataclass(frozen=True)
class AcquiredWallet:
    wallet: str
    source: str
    confidence: float = 0.0
    discovery_score: int = 25
    evidence_count: int = 1
    markets_seen: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AcquisitionSourceConfig:
    name: str
    enabled: bool = True
    limit: int = 50


class AcquisitionSource(Protocol):
    name: str

    def fetch(self, *, limit: int) -> list[AcquiredWallet]:
        ...


@dataclass
class DiscoveryDbAcquisitionSource:
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB
    name: str = "discovery_db"

    def fetch(self, *, limit: int) -> list[AcquiredWallet]:
        candidates = load_discovered_wallets(db_path=self.discovery_db_path, limit=limit)
        return [
            AcquiredWallet(
                wallet=candidate.wallet,
                source=self.name,
                confidence=0.5,
                discovery_score=int(candidate.discovery_score),
                evidence_count=int(candidate.evidence_count),
                markets_seen=list(candidate.markets_seen or []),
                metadata={"discovery_source": candidate.source},
            )
            for candidate in candidates
        ]


@dataclass
class RegistryAcquisitionSource:
    traders_db_path: str | Path = DEFAULT_TRADERS_DB
    name: str = "registry"

    def fetch(self, *, limit: int) -> list[AcquiredWallet]:
        rows = list_traders(limit=limit, db_path=str(self.traders_db_path))
        return [
            AcquiredWallet(
                wallet=row.wallet,
                source=self.name,
                confidence=float(row.confidence),
                discovery_score=max(1, int(row.watch_score)),
                evidence_count=1,
                metadata={
                    "classification": row.classification,
                    "first_seen": row.first_seen,
                    "last_seen": row.last_seen,
                    "watch_score": row.watch_score,
                },
            )
            for row in rows
        ]


@dataclass
class ForensicsAcquisitionSource:
    traders_db_path: str | Path = DEFAULT_TRADERS_DB
    name: str = "forensics"

    def fetch(self, *, limit: int) -> list[AcquiredWallet]:
        path = Path(self.traders_db_path)
        if not path.exists():
            return []
        with closing_connection(path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, classification, confidence, report_json
                FROM wallet_reports
                ORDER BY report_timestamp DESC
                LIMIT ?
                """,
                (max(int(limit), 0),),
            ).fetchall()
        acquired: list[AcquiredWallet] = []
        seen: set[str] = set()
        for row in rows:
            wallet = str(row["wallet"]).lower()
            if wallet in seen:
                continue
            seen.add(wallet)
            report = json.loads(row["report_json"] or "{}")
            metrics = report.get("metrics") or {}
            acquired.append(
                AcquiredWallet(
                    wallet=wallet,
                    source=self.name,
                    confidence=float(row["confidence"] or 0.0),
                    discovery_score=max(1, int(metrics.get("trade_count") or 1)),
                    evidence_count=int(metrics.get("trade_count") or 1),
                    metadata={
                        "classification": row["classification"],
                        "metrics": metrics,
                    },
                )
            )
        return acquired


@dataclass
class WatchlistAcquisitionSource:
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH
    name: str = "watchlist"

    def fetch(self, *, limit: int) -> list[AcquiredWallet]:
        wallets = discover_wallets(watchlist=self.watchlist_path, limit=limit)
        return [
            AcquiredWallet(
                wallet=wallet,
                source=self.name,
                confidence=0.4,
                discovery_score=30,
                evidence_count=1,
            )
            for wallet in wallets
        ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def init_wallet_acquisition_db(
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> None:
    init_wallet_discovery_db(traders_db_path, discovery_db_path)
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_acquisition_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                discovered INTEGER NOT NULL DEFAULT 0,
                accepted INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS wallet_acquisition_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                quality_score REAL NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_acquisition_records_wallet ON wallet_acquisition_records(wallet, recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_acquisition_records_status ON wallet_acquisition_records(status);
            CREATE INDEX IF NOT EXISTS idx_wallet_acquisition_records_source ON wallet_acquisition_records(source);
            CREATE INDEX IF NOT EXISTS idx_wallet_acquisition_runs_time ON wallet_acquisition_runs(finished_at DESC);
            """
        )


def _dedupe_acquired(candidates: list[AcquiredWallet]) -> list[AcquiredWallet]:
    merged: dict[str, AcquiredWallet] = {}
    for candidate in candidates:
        wallet = _normalize_wallet(candidate.wallet)
        if not wallet:
            continue
        existing = merged.get(wallet)
        if existing is None:
            merged[wallet] = AcquiredWallet(
                wallet=wallet,
                source=candidate.source,
                confidence=candidate.confidence,
                discovery_score=candidate.discovery_score,
                evidence_count=candidate.evidence_count,
                markets_seen=list(candidate.markets_seen),
                metadata=dict(candidate.metadata),
            )
            continue
        sources = set(str(existing.metadata.get("sources") or existing.source).split(","))
        sources.add(candidate.source)
        merged[wallet] = AcquiredWallet(
            wallet=wallet,
            source=",".join(sorted(sources)),
            confidence=max(existing.confidence, candidate.confidence),
            discovery_score=max(existing.discovery_score, candidate.discovery_score),
            evidence_count=existing.evidence_count + candidate.evidence_count,
            markets_seen=sorted(set(existing.markets_seen + candidate.markets_seen)),
            metadata={**existing.metadata, **candidate.metadata, "sources": ",".join(sorted(sources))},
        )
    return list(merged.values())


class WalletDataAcquisitionEngine:
    """Discover, validate, normalize, filter, and enrich wallet candidates."""

    def __init__(
        self,
        *,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
        sources: list[AcquisitionSourceConfig] | None = None,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        quality_filter: Any | None = None,
        discovery_engine: WalletDiscoveryEngine | None = None,
        tracker: WalletTracker | None = None,
    ) -> None:
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.watchlist_path = Path(watchlist_path)
        self.sources_config = sources or [
            AcquisitionSourceConfig(name="discovery_db", enabled=True, limit=100),
            AcquisitionSourceConfig(name="registry", enabled=True, limit=100),
            AcquisitionSourceConfig(name="forensics", enabled=True, limit=100),
            AcquisitionSourceConfig(name="watchlist", enabled=True, limit=50),
        ]
        self.rate_limit_seconds = rate_limit_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_retries = max_retries
        self._sleep = sleep
        self._cache: dict[str, tuple[float, list[AcquiredWallet]]] = {}
        self._discovery = discovery_engine or WalletDiscoveryEngine(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
            watchlist_path=self.watchlist_path,
        )
        self._tracker = tracker or WalletTracker(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
            watchlist_path=self.watchlist_path,
        )
        from src.intelligence.wallet_quality_filter import WalletQualityFilter

        self._quality_filter = quality_filter or WalletQualityFilter()
        init_wallet_acquisition_db(self.traders_db_path, self.discovery_db_path)

    def _build_sources(self) -> list[AcquisitionSource]:
        sources: list[AcquisitionSource] = []
        for cfg in self.sources_config:
            if not cfg.enabled:
                continue
            if cfg.name == "discovery_db":
                sources.append(DiscoveryDbAcquisitionSource(discovery_db_path=self.discovery_db_path))
            elif cfg.name == "registry":
                sources.append(RegistryAcquisitionSource(traders_db_path=self.traders_db_path))
            elif cfg.name == "forensics":
                sources.append(ForensicsAcquisitionSource(traders_db_path=self.traders_db_path))
            elif cfg.name == "watchlist":
                sources.append(WatchlistAcquisitionSource(watchlist_path=self.watchlist_path))
        return sources

    def _cache_get(self, key: str) -> list[AcquiredWallet] | None:
        cached = self._cache.get(key)
        if cached is None:
            return None
        ts, rows = cached
        if time.time() - ts > self.cache_ttl_seconds:
            return None
        return rows

    def _cache_set(self, key: str, rows: list[AcquiredWallet]) -> None:
        self._cache[key] = (time.time(), rows)

    def _fetch_with_retry(self, source: AcquisitionSource, *, limit: int) -> list[AcquiredWallet]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return source.fetch(limit=limit)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self._sleep(self.rate_limit_seconds * (attempt + 1))
        if last_error is not None:
            raise last_error
        return []

    def discover_candidates(self, *, limit: int | None = None) -> list[AcquiredWallet]:
        merged: list[AcquiredWallet] = []
        for cfg in self.sources_config:
            if not cfg.enabled:
                continue
            cache_key = f"{cfg.name}:{cfg.limit}"
            cached = self._cache_get(cache_key)
            if cached is not None:
                merged.extend(cached)
                continue
            source_map = {source.name: source for source in self._build_sources()}
            source = source_map.get(cfg.name)
            if source is None:
                continue
            rows = self._fetch_with_retry(source, limit=cfg.limit if limit is None else min(cfg.limit, limit))
            self._cache_set(cache_key, rows)
            merged.extend(rows)
            if self.rate_limit_seconds > 0:
                self._sleep(self.rate_limit_seconds)
        deduped = _dedupe_acquired(merged)
        if limit is not None:
            return deduped[:limit]
        return deduped

    def validate_wallet_address(self, wallet: str) -> bool:
        wallet = _normalize_wallet(wallet)
        if not WALLET_RE.match(wallet):
            return False
        try:
            validate_wallet(wallet)
            return True
        except ValueError:
            return False

    def normalize_record(self, acquired: AcquiredWallet) -> dict[str, Any]:
        return {
            "wallet": _normalize_wallet(acquired.wallet),
            "source": acquired.source,
            "confidence": round(float(acquired.confidence), 4),
            "discovery_score": int(acquired.discovery_score),
            "evidence_count": int(acquired.evidence_count),
            "markets_seen": list(acquired.markets_seen),
            "activity_level": int(acquired.metadata.get("trade_count") or acquired.evidence_count),
            "market_participation": len(acquired.markets_seen),
            "first_seen": acquired.metadata.get("first_seen"),
            "last_seen": acquired.metadata.get("last_seen"),
            "acquisition_source": acquired.source,
            "metadata": acquired.metadata,
        }

    def enrich_wallet(self, acquired: AcquiredWallet) -> dict[str, Any]:
        wallet = _normalize_wallet(acquired.wallet)
        record = load_wallet_report(wallet, db_path=str(self.traders_db_path))
        if record is not None:
            report = record.to_dict().get("report") or {}
            metrics = report.get("metrics") or acquired.metadata.get("metrics") or {}
            return {
                "wallet": wallet,
                "classification": report.get("classification") or acquired.metadata.get("classification") or "unknown",
                "confidence": float(report.get("confidence") or acquired.confidence),
                "watch_score": calculate_watch_score(
                    str(report.get("classification") or "unknown"),
                    float(report.get("confidence") or acquired.confidence),
                    metrics,
                    report.get("signals") or [],
                ),
                "metrics": metrics,
                "signals": report.get("signals") or [],
                "acquisition_source": acquired.source,
            }
        metrics = dict(acquired.metadata.get("metrics") or {})
        metrics.setdefault("trade_count", acquired.evidence_count)
        metrics.setdefault("markets_traded", len(acquired.markets_seen))
        classification = str(acquired.metadata.get("classification") or "unknown")
        confidence = float(acquired.confidence)
        return {
            "wallet": wallet,
            "classification": classification,
            "confidence": confidence,
            "watch_score": calculate_watch_score(classification, confidence, metrics, []),
            "metrics": metrics,
            "signals": [],
            "acquisition_source": acquired.source,
        }

    def _persist_acquisition_record(
        self,
        *,
        wallet: str,
        source: str,
        status: str,
        quality_score: float,
        confidence: float,
        reason: str,
        metadata: dict[str, Any],
    ) -> None:
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_acquisition_records (
                    wallet, source, status, quality_score, confidence, reason, metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wallet,
                    source,
                    status,
                    quality_score,
                    confidence,
                    reason,
                    json.dumps(metadata, sort_keys=True),
                    _utc_now(),
                ),
            )

    def _enrich_registry(self, acquired: AcquiredWallet, enriched: dict[str, Any]) -> None:
        save_wallet_report(enriched, watch_score=int(enriched.get("watch_score") or 0), db_path=str(self.traders_db_path))
        save_discovery_candidate(
            TraderDiscoveryCandidate(
                wallet=enriched["wallet"],
                source=acquired.source,
                discovery_score=int(acquired.discovery_score),
                evidence_count=int(acquired.evidence_count),
                markets_seen=list(acquired.markets_seen),
            ),
            db_path=self.discovery_db_path,
        )

    def acquire_wallet(self, acquired: AcquiredWallet) -> dict[str, Any]:
        normalized = self.normalize_record(acquired)
        wallet = normalized["wallet"]
        if not self.validate_wallet_address(wallet):
            quality = self._quality_filter.score_wallet(wallet, normalized, valid=False, reason="malformed wallet address")
            self._persist_acquisition_record(
                wallet=wallet or str(acquired.wallet),
                source=acquired.source,
                status=quality.status,
                quality_score=quality.quality_score,
                confidence=quality.confidence,
                reason=quality.reason,
                metadata=normalized,
            )
            return {"wallet": wallet, "status": quality.status, "reason": quality.reason, "quality": quality.to_dict()}

        quality = self._quality_filter.score_wallet(wallet, normalized)
        if quality.status == "rejected":
            self._persist_acquisition_record(
                wallet=wallet,
                source=acquired.source,
                status=quality.status,
                quality_score=quality.quality_score,
                confidence=quality.confidence,
                reason=quality.reason,
                metadata=normalized,
            )
            return {"wallet": wallet, "status": quality.status, "reason": quality.reason, "quality": quality.to_dict()}

        enriched = self.enrich_wallet(acquired)
        self._enrich_registry(acquired, enriched)
        self._persist_acquisition_record(
            wallet=wallet,
            source=acquired.source,
            status=quality.status,
            quality_score=quality.quality_score,
            confidence=quality.confidence,
            reason=quality.reason,
            metadata={**normalized, "enriched": enriched},
        )
        return {"wallet": wallet, "status": quality.status, "reason": quality.reason, "quality": quality.to_dict(), "enriched": True}

    def run_acquisition(self, *, limit: int = 100) -> dict[str, Any]:
        started = _utc_now()
        start_ts = time.time()
        candidates = self.discover_candidates(limit=limit)
        accepted = 0
        rejected = 0
        probation = 0
        synthetic_rejected = 0
        results: list[dict[str, Any]] = []
        for acquired in candidates:
            outcome = self.acquire_wallet(acquired)
            results.append(outcome)
            status = outcome.get("status")
            reason = str(outcome.get("reason") or "")
            if "synthetic wallet" in reason:
                synthetic_rejected += 1
            if status == "accepted":
                accepted += 1
            elif status == "probation":
                probation += 1
            else:
                rejected += 1

        watchlist = self._tracker.build_ranked_watchlist(limit=min(50, limit))
        self._tracker.persist_watchlist(watchlist)
        finished = _utc_now()
        duration_ms = round((time.time() - start_ts) * 1000.0, 2)
        payload = _with_flags(
            {
                "started_at": started,
                "finished_at": finished,
                "duration_ms": duration_ms,
                "discovered": len(candidates),
                "accepted": accepted,
                "rejected": rejected,
                "probation": probation,
                "synthetic_rejected": synthetic_rejected,
                "watchlist_count": len(watchlist),
                "results": results[:25],
            }
        )
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_acquisition_runs (
                    started_at, finished_at, duration_ms, discovered, accepted, rejected, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started,
                    finished,
                    duration_ms,
                    len(candidates),
                    accepted,
                    rejected,
                    json.dumps({"probation": probation, "watchlist_count": len(watchlist)}, sort_keys=True),
                ),
            )
        return payload

    def load_recent_records(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = """
            SELECT wallet, source, status, quality_score, confidence, reason, metadata_json, recorded_at
            FROM wallet_acquisition_records
        """
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        params.append(max(int(limit), 0))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in rows
        ]


def run_wallet_acquisition(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int = 100,
) -> dict[str, Any]:
    engine = WalletDataAcquisitionEngine(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    return engine.run_acquisition(limit=limit)
