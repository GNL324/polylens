from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, load_watched_wallets
from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR, DEFAULT_WATCHLIST_PATH, discover_wallets
from src.analysis.wallet_activity import (
    DEFAULT_WALLET_ACTIVITY_DB,
    WalletActivityExport,
    WalletActivitySource,
    export_wallet_activity,
    write_wallet_activity_export,
)
from src.sqlite_utils import closing_connection

DEFAULT_WATCHLIST_LIMIT = 50


@dataclass
class WalletWatchlistEntry:
    wallet: str
    composite_score: float
    watch_score: int
    alpha_score: int
    discovery_score: int
    classification: str
    rank: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActivityExporter(Protocol):
    def __call__(
        self,
        wallet: str,
        limit: int | None = None,
        source: WalletActivitySource | None = None,
        db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB,
        store: bool = True,
    ) -> WalletActivityExport:
        ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def _dedupe_wallets(wallets: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for wallet in wallets:
        normalized = _normalize_wallet(wallet)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def init_wallet_watchlist_db(db_path: str | Path = DEFAULT_TRADERS_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_watchlist (
                wallet TEXT PRIMARY KEY,
                composite_score REAL NOT NULL,
                watch_score INTEGER NOT NULL,
                alpha_score INTEGER NOT NULL,
                discovery_score INTEGER NOT NULL,
                classification TEXT NOT NULL,
                rank INTEGER NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_watchlist_rank ON wallet_watchlist(rank ASC);
            CREATE INDEX IF NOT EXISTS idx_wallet_watchlist_score ON wallet_watchlist(composite_score DESC);
            """
        )


def _latest_event_timestamp(wallet: str, db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB) -> float | None:
    wallet = _normalize_wallet(wallet)
    path = Path(db_path)
    if not path.exists():
        return None
    with closing_connection(path) as conn:
        row = conn.execute(
            "SELECT MAX(timestamp) AS latest FROM wallet_events WHERE wallet = ?",
            (wallet,),
        ).fetchone()
    if row is None or row["latest"] is None:
        return None
    return float(row["latest"])


class WalletTracker:
    """Discover, refresh, score, and rank wallets using existing Polylens storage."""

    def __init__(
        self,
        *,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        wallet_activity_db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB,
        paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
        wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
        watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
        activity_exporter: ActivityExporter | None = None,
    ) -> None:
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.wallet_activity_db_path = Path(wallet_activity_db_path)
        self.paper_copy_db_path = Path(paper_copy_db_path)
        self.wallet_export_dir = Path(wallet_export_dir)
        self.watchlist_path = Path(watchlist_path)
        self._export_activity = activity_exporter or export_wallet_activity

    def discover_top_wallets(self, *, limit: int = DEFAULT_WATCHLIST_LIMIT) -> list[str]:
        manual = discover_wallets(
            watchlist=self.watchlist_path,
            registry_db_path=self.traders_db_path,
            include_registry=True,
            limit=limit,
        )
        watched = load_watched_wallets(db_path=self.paper_copy_db_path)
        discovered = [candidate.wallet for candidate in load_discovered_wallets(db_path=self.discovery_db_path, limit=limit)]
        registry = [row.wallet for row in list_traders(limit=limit, db_path=str(self.traders_db_path))]
        return _dedupe_wallets(manual + watched + discovered + registry)[:limit]

    def refresh_wallet_history(
        self,
        wallet: str,
        *,
        limit: int | None = 500,
        source: WalletActivitySource | None = None,
        incremental: bool = True,
    ) -> WalletActivityExport:
        wallet = _normalize_wallet(wallet)
        if incremental:
            latest = _latest_event_timestamp(wallet, db_path=self.wallet_activity_db_path)
            if latest is not None:
                limit = limit or 500
        export = self._export_activity(
            wallet,
            limit=limit,
            source=source,
            db_path=self.wallet_activity_db_path,
            store=True,
        )
        export_path = self.wallet_export_dir / f"{wallet}_activity.json"
        write_wallet_activity_export(export, export_path)
        return export

    def refresh_wallets(
        self,
        wallets: list[str] | None = None,
        *,
        limit: int | None = 500,
        source: WalletActivitySource | None = None,
    ) -> list[dict[str, Any]]:
        targets = wallets or self.discover_top_wallets()
        results: list[dict[str, Any]] = []
        for wallet in targets:
            export = self.refresh_wallet_history(wallet, limit=limit, source=source)
            results.append(
                {
                    "wallet": wallet,
                    "event_count": export.event_count,
                    "export_timestamp": export.export_timestamp,
                    "export_path": str(self.wallet_export_dir / f"{wallet}_activity.json"),
                    "latest_event_timestamp": _latest_event_timestamp(wallet, db_path=self.wallet_activity_db_path),
                }
            )
        return results

    def score_wallet(self, wallet: str) -> dict[str, Any]:
        wallet = _normalize_wallet(wallet)
        alpha = build_trader_alpha_report(
            wallet,
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
        )
        discovery_score = 0
        for candidate in load_discovered_wallets(db_path=self.discovery_db_path):
            if candidate.wallet == wallet:
                discovery_score = int(candidate.discovery_score)
                break
        composite = round(
            alpha.watch_score * 0.4 + alpha.alpha_score * 0.4 + discovery_score * 0.2,
            4,
        )
        return {
            "wallet": wallet,
            "composite_score": composite,
            "watch_score": alpha.watch_score,
            "alpha_score": alpha.alpha_score,
            "discovery_score": discovery_score,
            "classification": alpha.classification,
        }

    def build_ranked_watchlist(self, *, limit: int = DEFAULT_WATCHLIST_LIMIT) -> list[WalletWatchlistEntry]:
        wallets = self.discover_top_wallets(limit=limit)
        scored = [self.score_wallet(wallet) for wallet in wallets]
        scored.sort(
            key=lambda row: (
                -float(row["composite_score"]),
                -int(row["alpha_score"]),
                -int(row["watch_score"]),
                row["wallet"],
            )
        )
        entries: list[WalletWatchlistEntry] = []
        for index, row in enumerate(scored[:limit], start=1):
            entries.append(
                WalletWatchlistEntry(
                    wallet=row["wallet"],
                    composite_score=float(row["composite_score"]),
                    watch_score=int(row["watch_score"]),
                    alpha_score=int(row["alpha_score"]),
                    discovery_score=int(row["discovery_score"]),
                    classification=str(row["classification"]),
                    rank=index,
                    source="wallet_intelligence",
                )
            )
        return entries

    def persist_watchlist(self, entries: list[WalletWatchlistEntry] | None = None, *, limit: int = DEFAULT_WATCHLIST_LIMIT) -> dict[str, Any]:
        init_wallet_watchlist_db(self.traders_db_path)
        entries = entries or self.build_ranked_watchlist(limit=limit)
        now = _utc_now()
        with closing_connection(self.traders_db_path) as conn:
            conn.execute("DELETE FROM wallet_watchlist")
            for entry in entries:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO wallet_watchlist (
                        wallet, composite_score, watch_score, alpha_score, discovery_score,
                        classification, rank, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.wallet,
                        entry.composite_score,
                        entry.watch_score,
                        entry.alpha_score,
                        entry.discovery_score,
                        entry.classification,
                        entry.rank,
                        entry.source,
                        now,
                    ),
                )
        return {
            "watchlist_count": len(entries),
            "updated_at": now,
            "entries": [entry.to_dict() for entry in entries],
        }

    def load_watchlist(self, *, limit: int = DEFAULT_WATCHLIST_LIMIT) -> list[WalletWatchlistEntry]:
        init_wallet_watchlist_db(self.traders_db_path)
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(
                """
                SELECT wallet, composite_score, watch_score, alpha_score, discovery_score,
                       classification, rank, source
                FROM wallet_watchlist
                ORDER BY rank ASC
                LIMIT ?
                """,
                (max(int(limit), 0),),
            ).fetchall()
        if not rows:
            return self.build_ranked_watchlist(limit=limit)
        return [
            WalletWatchlistEntry(
                wallet=str(row["wallet"]),
                composite_score=float(row["composite_score"]),
                watch_score=int(row["watch_score"]),
                alpha_score=int(row["alpha_score"]),
                discovery_score=int(row["discovery_score"]),
                classification=str(row["classification"]),
                rank=int(row["rank"]),
                source=str(row["source"]),
            )
            for row in rows
        ]

    def activity_export_paths(self, wallets: list[str] | None = None) -> list[Path]:
        targets = wallets or [entry.wallet for entry in self.load_watchlist()]
        paths: list[Path] = []
        for wallet in targets:
            path = self.wallet_export_dir / f"{_normalize_wallet(wallet)}_activity.json"
            if path.exists():
                paths.append(path)
        return paths
