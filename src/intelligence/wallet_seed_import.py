from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    registry_stats,
    save_wallet_report,
    top_traders,
)
from src.analysis.trader_scanner import DEFAULT_WATCHLIST_PATH, DEFAULT_WALLET_EXPORT_DIR
from src.analysis.wallet_activity import validate_wallet
from src.analysis.wallet_forensics import build_wallet_forensics_report
from src.intelligence.wallet_data_acquisition import (
    AcquiredWallet,
    WalletDataAcquisitionEngine,
    init_wallet_acquisition_db,
)
from src.intelligence.wallet_discovery import init_wallet_discovery_db
from src.intelligence.wallet_tracker import WalletTracker
from src.sqlite_utils import closing_connection

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DEFAULT_SEED_WALLETS_PATH = "data/traders/seed_wallets.json"
DEFAULT_SEED_EXPORTS_DIR = "data/traders/seed_exports"
DEFAULT_REGISTRY_SNAPSHOTS_DIR = "data/traders/registry_snapshots"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, **payload}


def init_wallet_seed_import_db(traders_db_path: str | Path = DEFAULT_TRADERS_DB) -> None:
    init_wallet_discovery_db(traders_db_path)
    with closing_connection(traders_db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_seed_imports (
                wallet TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                imported_at TEXT NOT NULL,
                PRIMARY KEY (wallet, source)
            );
            CREATE TABLE IF NOT EXISTS wallet_bootstrap_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                status TEXT NOT NULL,
                imported_count INTEGER NOT NULL DEFAULT 0,
                skipped_duplicates INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_seed_imports_wallet ON wallet_seed_imports(wallet, imported_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_bootstrap_runs_time ON wallet_bootstrap_runs(finished_at DESC);
            """
        )


@dataclass
class SeedImportResult:
    wallet: str
    source: str
    imported: bool
    skipped: bool
    reason: str = ""
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WalletSeedImporter:
    """Import seed wallets and offline forensic material into registry and discovery."""

    def __init__(
        self,
        *,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
        export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
        seed_wallets_path: str | Path = DEFAULT_SEED_WALLETS_PATH,
        seed_exports_dir: str | Path = DEFAULT_SEED_EXPORTS_DIR,
        registry_snapshots_dir: str | Path = DEFAULT_REGISTRY_SNAPSHOTS_DIR,
    ) -> None:
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.watchlist_path = Path(watchlist_path)
        self.export_dir = Path(export_dir)
        self.seed_wallets_path = Path(seed_wallets_path)
        self.seed_exports_dir = Path(seed_exports_dir)
        self.registry_snapshots_dir = Path(registry_snapshots_dir)
        init_wallet_seed_import_db(self.traders_db_path)

    def _known_wallets(self) -> set[str]:
        known = {_normalize_wallet(row.wallet) for row in list_traders(limit=10_000, db_path=str(self.traders_db_path))}
        for candidate in load_discovered_wallets(db_path=self.discovery_db_path, limit=10_000):
            known.add(_normalize_wallet(candidate.wallet))
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute("SELECT wallet FROM wallet_seed_imports").fetchall()
        known.update(_normalize_wallet(row["wallet"]) for row in rows)
        return known

    def _persist_seed_import(
        self,
        *,
        wallet: str,
        source: str,
        confidence: float,
        metadata: dict[str, Any],
    ) -> None:
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT INTO wallet_seed_imports (wallet, source, confidence, metadata_json, imported_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(wallet, source) DO UPDATE SET
                    confidence = excluded.confidence,
                    metadata_json = excluded.metadata_json,
                    imported_at = excluded.imported_at
                """,
                (wallet, source, confidence, json.dumps(metadata, sort_keys=True), _utc_now()),
            )

    def _materialize_report(
        self,
        report_payload: dict[str, Any],
        *,
        source: str,
        confidence: float,
        discovery_score: int = 35,
        evidence_count: int = 1,
        markets_seen: list[str] | None = None,
    ) -> SeedImportResult:
        wallet = _normalize_wallet(str(report_payload.get("wallet") or ""))
        if not WALLET_RE.match(wallet):
            return SeedImportResult(wallet=wallet, source=source, imported=False, skipped=True, reason="invalid wallet")
        known = self._known_wallets()
        if wallet in known:
            return SeedImportResult(wallet=wallet, source=source, imported=False, skipped=True, reason="duplicate")

        watch_score = int(
            report_payload.get("watch_score")
            or calculate_watch_score(
                str(report_payload.get("classification") or "unknown"),
                float(report_payload.get("confidence") or confidence),
                report_payload.get("metrics") or {},
                report_payload.get("signals") or [],
            )
        )
        save_wallet_report(report_payload, watch_score=watch_score, db_path=str(self.traders_db_path))
        save_discovery_candidate(
            TraderDiscoveryCandidate(
                wallet=wallet,
                source=source,
                discovery_score=discovery_score,
                evidence_count=evidence_count,
                markets_seen=list(markets_seen or []),
            ),
            db_path=self.discovery_db_path,
        )
        self._append_watchlist(wallet)
        metadata = {
            "classification": report_payload.get("classification"),
            "watch_score": watch_score,
            "confidence": confidence,
        }
        self._persist_seed_import(wallet=wallet, source=source, confidence=confidence, metadata=metadata)
        return SeedImportResult(
            wallet=wallet,
            source=source,
            imported=True,
            skipped=False,
            confidence=confidence,
            metadata=metadata,
        )

    def _append_watchlist(self, wallet: str) -> None:
        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if self.watchlist_path.exists():
            payload = json.loads(self.watchlist_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                existing = [_normalize_wallet(item) for item in payload if _normalize_wallet(item)]
        merged = sorted(set(existing + [_normalize_wallet(wallet)]))
        self.watchlist_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    def import_known_wallets(
        self,
        wallets: list[str],
        *,
        source: str = "seed_list",
        confidence: float = 0.35,
    ) -> list[SeedImportResult]:
        results: list[SeedImportResult] = []
        for raw in wallets:
            wallet = _normalize_wallet(raw)
            if not WALLET_RE.match(wallet):
                results.append(
                    SeedImportResult(wallet=wallet, source=source, imported=False, skipped=True, reason="invalid wallet")
                )
                continue
            if wallet in self._known_wallets():
                results.append(
                    SeedImportResult(wallet=wallet, source=source, imported=False, skipped=True, reason="duplicate")
                )
                continue
            report_payload = {
                "wallet": wallet,
                "classification": "unknown",
                "confidence": confidence,
                "metrics": {"trade_count": 0, "markets_traded": 0},
                "signals": [],
            }
            results.append(
                self._materialize_report(
                    report_payload,
                    source=source,
                    confidence=confidence,
                    discovery_score=25,
                    evidence_count=0,
                )
            )
        return results

    def import_wallet_export(self, path: str | Path, *, source: str = "wallet_export") -> SeedImportResult:
        export_path = Path(path)
        if not export_path.exists():
            return SeedImportResult(wallet="", source=source, imported=False, skipped=True, reason="missing export")
        try:
            payload = json.loads(export_path.read_text(encoding="utf-8"))
            wallet = _normalize_wallet(str(payload.get("wallet") or export_path.stem.replace("_activity", "")))
            validate_wallet(wallet)
            report = build_wallet_forensics_report(export_path, wallet=wallet)
            report_payload = report.to_dict()
            metrics = report_payload.get("metrics") or {}
            return self._materialize_report(
                report_payload,
                source=source,
                confidence=float(report.confidence),
                discovery_score=max(25, int(metrics.get("trade_count") or 1)),
                evidence_count=int(metrics.get("trade_count") or 1),
            )
        except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            return SeedImportResult(wallet="", source=source, imported=False, skipped=True, reason=str(exc))

    def import_forensic_report(self, path: str | Path, *, source: str = "forensic_report") -> SeedImportResult:
        report_path = Path(path)
        if not report_path.exists():
            return SeedImportResult(wallet="", source=source, imported=False, skipped=True, reason="missing report")
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if "activities" in payload:
                return self.import_wallet_export(report_path, source=source)
            wallet = _normalize_wallet(str(payload.get("wallet") or ""))
            if not wallet:
                return SeedImportResult(wallet="", source=source, imported=False, skipped=True, reason="missing wallet")
            metrics = payload.get("metrics") or {}
            return self._materialize_report(
                payload,
                source=source,
                confidence=float(payload.get("confidence") or 0.5),
                discovery_score=max(25, int(metrics.get("trade_count") or 1)),
                evidence_count=int(metrics.get("trade_count") or 1),
            )
        except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            return SeedImportResult(wallet="", source=source, imported=False, skipped=True, reason=str(exc))

    def import_registry_snapshot(self, path: str | Path, *, source: str = "registry_snapshot") -> list[SeedImportResult]:
        snapshot_path = Path(path)
        if not snapshot_path.exists():
            return []
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        entries = payload.get("wallets") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return []
        results: list[SeedImportResult] = []
        for entry in entries:
            if isinstance(entry, str):
                results.extend(self.import_known_wallets([entry], source=source, confidence=0.4))
                continue
            if not isinstance(entry, dict):
                continue
            wallet = _normalize_wallet(str(entry.get("wallet") or ""))
            report_payload = {
                "wallet": wallet,
                "classification": entry.get("classification") or "unknown",
                "confidence": float(entry.get("confidence") or 0.4),
                "metrics": entry.get("metrics") or {},
                "signals": entry.get("signals") or [],
                "watch_score": entry.get("watch_score"),
            }
            results.append(
                self._materialize_report(
                    report_payload,
                    source=source,
                    confidence=float(entry.get("confidence") or 0.4),
                )
            )
        return results

    def import_from_seed_file(self, path: str | Path | None = None, *, source: str = "seed_file") -> list[SeedImportResult]:
        seed_path = Path(path or self.seed_wallets_path)
        if not seed_path.exists():
            return []
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            wallets = payload.get("wallets") or []
        elif isinstance(payload, list):
            wallets = payload
        else:
            return []
        return self.import_known_wallets([str(item) for item in wallets], source=source, confidence=0.35)

    def import_directory_exports(self, directory: str | Path | None = None) -> list[SeedImportResult]:
        root = Path(directory or self.seed_exports_dir)
        if not root.exists():
            return []
        results: list[SeedImportResult] = []
        for path in sorted(root.glob("*.json")):
            results.append(self.import_forensic_report(path, source="seed_export"))
        return results

    def import_directory_snapshots(self, directory: str | Path | None = None) -> list[SeedImportResult]:
        root = Path(directory or self.registry_snapshots_dir)
        if not root.exists():
            return []
        results: list[SeedImportResult] = []
        for path in sorted(root.glob("*.json")):
            results.extend(self.import_registry_snapshot(path))
        return results

    def import_existing_wallet_exports(self) -> list[SeedImportResult]:
        if not self.export_dir.exists():
            return []
        results: list[SeedImportResult] = []
        for path in sorted(self.export_dir.glob("*_activity.json")):
            result = self.import_wallet_export(path, source="existing_export")
            if result.imported or result.wallet:
                results.append(result)
        return results

    def bootstrap_from_packaged_seeds(self) -> dict[str, Any]:
        batches = {
            "seed_exports": self.import_directory_exports(),
            "registry_snapshots": self.import_directory_snapshots(),
            "existing_exports": self.import_existing_wallet_exports(),
            "seed_file": self.import_from_seed_file(),
        }
        all_results = [item for group in batches.values() for item in group]
        imported = [item for item in all_results if item.imported]
        skipped = [item for item in all_results if item.skipped]
        tracker = WalletTracker(
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
            watchlist_path=self.watchlist_path,
        )
        watchlist = tracker.persist_watchlist(limit=50)
        return _with_flags(
            {
                "imported_count": len(imported),
                "skipped_duplicates": len(skipped),
                "imported_wallets": sorted({item.wallet for item in imported}),
                "batches": {name: [row.to_dict() for row in rows] for name, rows in batches.items()},
                "watchlist": watchlist,
            }
        )


def ingest_top_traders_from_registry(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int = 25,
) -> dict[str, Any]:
    rows = top_traders(limit=limit, db_path=str(traders_db_path))
    ingested: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        wallet = _normalize_wallet(row.wallet)
        save_discovery_candidate(
            TraderDiscoveryCandidate(
                wallet=wallet,
                source="bootstrap_top_trader",
                discovery_score=max(1, 60 - index),
                evidence_count=1,
                markets_seen=[],
            ),
            db_path=discovery_db_path,
        )
        ingested.append(row.to_dict())
    return _with_flags({"ingested_count": len(ingested), "wallets": ingested})


def run_seed_acquisition(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
    limit: int = 100,
) -> dict[str, Any]:
    init_wallet_acquisition_db(traders_db_path, discovery_db_path)
    engine = WalletDataAcquisitionEngine(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        watchlist_path=watchlist_path,
        rate_limit_seconds=0.0,
        cache_ttl_seconds=0.0,
    )
    return engine.run_acquisition(limit=limit)


def ecosystem_is_empty(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> bool:
    init_wallet_acquisition_db(traders_db_path, discovery_db_path)
    stats = registry_stats(db_path=str(traders_db_path))
    registry_count = int(stats.get("total_traders") or 0)
    discovery_count = len(load_discovered_wallets(db_path=discovery_db_path, limit=1))
    with closing_connection(traders_db_path) as conn:
        acquisition_count = conn.execute("SELECT COUNT(*) AS count FROM wallet_acquisition_records").fetchone()["count"]
    return registry_count == 0 and discovery_count == 0 and int(acquisition_count) == 0


def bootstrap_health_report(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> dict[str, Any]:
    init_wallet_seed_import_db(traders_db_path)
    init_wallet_acquisition_db(traders_db_path, discovery_db_path)
    stats = registry_stats(db_path=str(traders_db_path))
    discovery_count = len(load_discovered_wallets(db_path=discovery_db_path, limit=10_000))
    with closing_connection(traders_db_path) as conn:
        seed_imported = conn.execute("SELECT COUNT(DISTINCT wallet) AS count FROM wallet_seed_imports").fetchone()["count"]
        acquisition_total = conn.execute("SELECT COUNT(*) AS count FROM wallet_acquisition_records").fetchone()["count"]
        accepted = conn.execute(
            "SELECT COUNT(*) AS count FROM wallet_acquisition_records WHERE status IN ('accepted', 'probation')"
        ).fetchone()["count"]
        bootstrap_runs = conn.execute("SELECT COUNT(*) AS count FROM wallet_bootstrap_runs").fetchone()["count"]
    ingestion_success_rate = round(float(accepted) / float(acquisition_total), 4) if acquisition_total else 0.0
    return _with_flags(
        {
            "seed_wallets_imported": int(seed_imported),
            "registry_population": int(stats.get("total_traders") or 0),
            "discovery_population": discovery_count,
            "wallet_acquisition_records": int(acquisition_total),
            "ingestion_success_rate": ingestion_success_rate,
            "bootstrap_runs": int(bootstrap_runs),
            "registry_growth": int(stats.get("total_traders") or 0),
            "ecosystem_empty": ecosystem_is_empty(
                traders_db_path=traders_db_path,
                discovery_db_path=discovery_db_path,
            ),
        }
    )


def run_wallet_bootstrap_cycle(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
    export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    seed_wallets_path: str | Path = DEFAULT_SEED_WALLETS_PATH,
    seed_exports_dir: str | Path = DEFAULT_SEED_EXPORTS_DIR,
    force: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    init_wallet_seed_import_db(traders_db_path)
    if not force and not ecosystem_is_empty(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path):
        health = bootstrap_health_report(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
        return _with_flags({"skipped": True, "reason": "ecosystem not empty", "health": health})

    started = _utc_now()
    start_ts = datetime.now(timezone.utc).timestamp()
    importer = WalletSeedImporter(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        watchlist_path=watchlist_path,
        export_dir=export_dir,
        seed_wallets_path=seed_wallets_path,
        seed_exports_dir=seed_exports_dir,
    )
    seed_result = importer.bootstrap_from_packaged_seeds()
    top_traders_result = ingest_top_traders_from_registry(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        limit=min(25, limit),
    )
    acquisition_result = run_seed_acquisition(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        watchlist_path=watchlist_path,
        limit=limit,
    )
    health = bootstrap_health_report(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    finished = _utc_now()
    duration_ms = round((datetime.now(timezone.utc).timestamp() - start_ts) * 1000.0, 2)
    payload = _with_flags(
        {
            "skipped": False,
            "seed_import": seed_result,
            "top_traders": top_traders_result,
            "acquisition": acquisition_result,
            "health": health,
        }
    )
    with closing_connection(traders_db_path) as conn:
        conn.execute(
            """
            INSERT INTO wallet_bootstrap_runs (
                started_at, finished_at, duration_ms, status, imported_count, skipped_duplicates, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started,
                finished,
                duration_ms,
                "success",
                int(seed_result.get("imported_count") or 0),
                int(seed_result.get("skipped_duplicates") or 0),
                json.dumps(payload, sort_keys=True),
            ),
        )
    return payload
