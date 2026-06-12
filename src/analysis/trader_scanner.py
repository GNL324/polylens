from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.analysis.trader_registry import (
    DEFAULT_TRADERS_DB,
    calculate_watch_score,
    list_traders,
    save_wallet_report,
    top_traders,
)
from src.analysis.wallet_activity import (
    DEFAULT_WALLET_ACTIVITY_DB,
    WalletActivitySource,
    export_wallet_activity,
    validate_wallet,
    write_wallet_activity_export,
)
from src.analysis.wallet_forensics import build_wallet_forensics_report

LOGGER = logging.getLogger(__name__)

DEFAULT_WATCHLIST_PATH = "data/traders/watchlist.json"
DEFAULT_WALLET_EXPORT_DIR = "data/wallets"
CLASSIFICATIONS = ("market_maker", "arbitrage_trader", "quantitative_directional", "mixed", "unknown")
SUMMARY_KEYS = {
    "market_maker": "market_makers",
    "arbitrage_trader": "arbitrage_traders",
    "quantitative_directional": "quantitative_directional",
    "mixed": "mixed",
    "unknown": "unknown",
}


class TraderWalletSource(Protocol):
    name: str

    def wallets(self, limit: int | None = None) -> list[str]:
        ...


@dataclass
class ManualWalletSource:
    path: str | Path = DEFAULT_WATCHLIST_PATH
    name: str = "manual-watchlist"

    def wallets(self, limit: int | None = None) -> list[str]:
        path = Path(self.path)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("watchlist JSON must be a list of wallet addresses")
        return _limit_wallets(_clean_wallets(payload), limit)


@dataclass
class RegistryWalletSource:
    db_path: str | Path = DEFAULT_TRADERS_DB
    name: str = "trader-registry"

    def wallets(self, limit: int | None = None) -> list[str]:
        rows = list_traders(limit=limit or 10_000, db_path=str(self.db_path))
        return _limit_wallets([row.wallet for row in rows], limit)


def discover_wallets(
    wallets: list[str] | None = None,
    watchlist: str | Path = DEFAULT_WATCHLIST_PATH,
    registry_db_path: str | Path = DEFAULT_TRADERS_DB,
    include_registry: bool = True,
    sources: list[TraderWalletSource] | None = None,
    limit: int | None = None,
) -> list[str]:
    if wallets:
        return _limit_wallets(_clean_wallets(wallets), limit)

    source_list = list(sources or [ManualWalletSource(watchlist)])
    if include_registry:
        source_list.append(RegistryWalletSource(registry_db_path))

    discovered: list[str] = []
    for source in source_list:
        try:
            discovered.extend(source.wallets(limit=limit))
        except Exception as exc:
            LOGGER.warning("wallet discovery source failed source=%s: %s", source.name, exc)
    return _limit_wallets(_clean_wallets(discovered), limit)


def scan_wallet(
    wallet: str,
    activity_limit: int | None = None,
    activity_source: WalletActivitySource | None = None,
    wallet_activity_db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    output_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    wallet = wallet.lower()
    try:
        validate_wallet(wallet)
        export = export_wallet_activity(
            wallet=wallet,
            limit=activity_limit,
            source=activity_source,
            db_path=wallet_activity_db_path,
            store=True,
        )
        output_path = Path(output_dir) / f"{wallet}_activity.json"
        write_wallet_activity_export(export, output_path)
        report = build_wallet_forensics_report(output_path, wallet=wallet)
        report_payload = report.to_dict()
        watch_score = calculate_watch_score(report.classification, report.confidence, report.metrics, report.signals)
        report_payload["watch_score"] = watch_score
        save_wallet_report(report_payload, watch_score=watch_score, db_path=str(traders_db_path))
        return {
            "accepted": True,
            "wallet": wallet,
            "classification": report.classification,
            "confidence": report.confidence,
            "watch_score": watch_score,
            "event_count": export.event_count,
            "activity_export": str(output_path),
        }
    except Exception as exc:
        LOGGER.warning("trader scan failed wallet=%s: %s", wallet, exc)
        return {"accepted": False, "wallet": wallet, "error": str(exc)}


def scan_wallets(
    wallets: list[str] | None = None,
    watchlist: str | Path = DEFAULT_WATCHLIST_PATH,
    limit: int | None = None,
    activity_limit: int | None = None,
    include_registry: bool = True,
    activity_source: WalletActivitySource | None = None,
    wallet_activity_db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    output_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    sources: list[TraderWalletSource] | None = None,
) -> dict[str, Any]:
    candidates = discover_wallets(
        wallets=wallets,
        watchlist=watchlist,
        registry_db_path=traders_db_path,
        include_registry=include_registry,
        sources=sources,
        limit=limit,
    )
    results = [
        scan_wallet(
            wallet,
            activity_limit=activity_limit,
            activity_source=activity_source,
            wallet_activity_db_path=wallet_activity_db_path,
            traders_db_path=traders_db_path,
            output_dir=output_dir,
        )
        for wallet in candidates
    ]
    return summarize_scan_results(results)


def summarize_scan_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "wallets_scanned": len(results),
        "scan_failures": sum(1 for result in results if not result.get("accepted")),
        "results": results,
    }
    for classification in CLASSIFICATIONS:
        summary[SUMMARY_KEYS[classification]] = 0
    for result in results:
        if not result.get("accepted"):
            continue
        classification = str(result.get("classification") or "unknown")
        key = SUMMARY_KEYS.get(classification, "unknown")
        summary[key] = int(summary.get(key, 0)) + 1
    return summary


def top_market_makers(limit: int = 20, db_path: str | Path = DEFAULT_TRADERS_DB) -> list[Any]:
    return top_traders(limit=limit, classification="market_maker", db_path=str(db_path))


def top_arbitrage_traders(limit: int = 20, db_path: str | Path = DEFAULT_TRADERS_DB) -> list[Any]:
    return top_traders(limit=limit, classification="arbitrage_trader", db_path=str(db_path))


def top_directional_traders(limit: int = 20, db_path: str | Path = DEFAULT_TRADERS_DB) -> list[Any]:
    return top_traders(limit=limit, classification="quantitative_directional", db_path=str(db_path))


def _clean_wallets(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        wallet = str(value or "").strip().lower()
        if not wallet or wallet in seen:
            continue
        validate_wallet(wallet)
        seen.add(wallet)
        cleaned.append(wallet)
    return cleaned


def _limit_wallets(wallets: list[str], limit: int | None = None) -> list[str]:
    if limit is None:
        return wallets
    return wallets[: max(0, int(limit))]
