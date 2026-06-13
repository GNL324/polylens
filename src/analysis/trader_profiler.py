from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report, rank_trader_alpha
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_network import build_trader_network, network_summary
from src.analysis.trader_registry import (
    DEFAULT_TRADERS_DB,
    calculate_watch_score,
    load_wallet_report,
    save_wallet_report,
)
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR, scan_wallet
from src.analysis.wallet_forensics import build_wallet_forensics_report

DEFAULT_PROFILE_LIMIT = 25


@dataclass
class TraderProfile:
    wallet: str
    profile: str
    classification: str
    confidence: float
    alpha_score: int
    watch_score: int
    markets_traded: int
    shared_markets: int
    btc_volume: float
    eth_volume: float
    sol_volume: float
    merge_count: int
    redeem_count: int
    specialization: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["profile"] = self.profile
        return payload


def profile_wallet(
    wallet: str,
    *,
    scan_if_missing: bool = True,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> TraderProfile:
    wallet = str(wallet or "").strip().lower()
    _ensure_wallet_analysis(
        wallet,
        scan_if_missing=scan_if_missing,
        traders_db_path=traders_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    alpha = build_trader_alpha_report(
        wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
    )
    specialization = derive_specialization(
        classification=alpha.classification,
        confidence=alpha.confidence,
        activity_count=alpha.activity_count,
        markets_traded=alpha.markets_traded,
        shared_markets=alpha.shared_markets,
        overlap_ratio=alpha.overlap_ratio,
        merge_count=alpha.merge_count,
        redeem_count=alpha.redeem_count,
        btc_volume=alpha.asset_breakdown.get("BTC", 0.0),
        eth_volume=alpha.asset_breakdown.get("ETH", 0.0),
        sol_volume=alpha.asset_breakdown.get("SOL", 0.0),
        other_volume=alpha.asset_breakdown.get("OTHER", 0.0),
    )
    return TraderProfile(
        wallet=wallet,
        profile=specialization,
        classification=alpha.classification,
        confidence=alpha.confidence,
        alpha_score=alpha.alpha_score,
        watch_score=alpha.watch_score,
        markets_traded=alpha.markets_traded,
        shared_markets=alpha.shared_markets,
        btc_volume=alpha.asset_breakdown.get("BTC", 0.0),
        eth_volume=alpha.asset_breakdown.get("ETH", 0.0),
        sol_volume=alpha.asset_breakdown.get("SOL", 0.0),
        merge_count=alpha.merge_count,
        redeem_count=alpha.redeem_count,
        specialization=specialization,
    )


def profile_traders(
    wallets: list[str] | None = None,
    *,
    wallet: str | None = None,
    top_connected: bool = False,
    top_alpha: bool = False,
    limit: int = DEFAULT_PROFILE_LIMIT,
    focus_wallet: str | None = None,
    scan_if_missing: bool = True,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> list[dict[str, Any]]:
    selected = select_profile_wallets(
        wallets=wallets,
        wallet=wallet,
        top_connected=top_connected,
        top_alpha=top_alpha,
        limit=limit,
        focus_wallet=focus_wallet,
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        wallet_export_dir=wallet_export_dir,
    )
    profiles = [
        profile_wallet(
            candidate,
            scan_if_missing=scan_if_missing,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        ).to_dict()
        for candidate in selected
    ]
    return profiles


def select_profile_wallets(
    *,
    wallets: list[str] | None = None,
    wallet: str | None = None,
    top_connected: bool = False,
    top_alpha: bool = False,
    limit: int = DEFAULT_PROFILE_LIMIT,
    focus_wallet: str | None = None,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> list[str]:
    if top_connected:
        network = build_trader_network(
            wallet=focus_wallet,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
            wallet_export_dir=wallet_export_dir,
        )
        summary = network_summary(network, limit=limit, focus_wallet=focus_wallet)
        return _dedupe_wallets(
            [row["wallet"] for row in summary["top_connected_wallets"]],
            limit=limit,
        )
    if top_alpha:
        ranked = rank_trader_alpha(
            limit=limit,
            traders_db_path=traders_db_path,
            discovery_db_path=discovery_db_path,
        )
        return _dedupe_wallets([row["wallet"] for row in ranked], limit=limit)
    if wallet:
        return [str(wallet).strip().lower()]
    if wallets:
        return _dedupe_wallets(wallets, limit=limit)
    discovered = load_discovered_wallets(discovery_db_path, limit=limit)
    return _dedupe_wallets([candidate.wallet for candidate in discovered], limit=limit)


def derive_specialization(
    *,
    classification: str,
    confidence: float,
    activity_count: int,
    markets_traded: int,
    shared_markets: int,
    overlap_ratio: float,
    merge_count: int,
    redeem_count: int,
    btc_volume: float,
    eth_volume: float,
    sol_volume: float,
    other_volume: float,
) -> str:
    crypto_volume = btc_volume + eth_volume + sol_volume
    total_volume = crypto_volume + other_volume
    inventory_ops = merge_count + redeem_count

    if classification == "arbitrage_trader" and (shared_markets >= 10 or overlap_ratio >= 0.25):
        return "cross-market arbitrage"

    if classification == "quantitative_directional":
        return "directional trader"

    if crypto_volume > 0:
        asset_volumes = {
            "btc": btc_volume,
            "eth": eth_volume,
            "sol": sol_volume,
        }
        top_asset, top_volume = max(asset_volumes.items(), key=lambda item: item[1])
        top_share = top_volume / crypto_volume
        if top_share >= 0.8:
            return f"{top_asset} specialist"

    if inventory_ops >= 20 and activity_count >= 100 and inventory_ops / max(activity_count, 1) >= 0.12:
        return "high-frequency inventory manager"

    if classification == "arbitrage_trader":
        return "cross-market arbitrage" if shared_markets >= 3 else "arbitrage trader"

    if classification == "market_maker" and crypto_volume >= 100:
        return "crypto market maker"

    if classification == "market_maker":
        return "crypto market maker" if crypto_volume > 0 else "market maker"

    if classification == "mixed":
        if shared_markets >= 5 and overlap_ratio >= 0.2:
            return "cross-market arbitrage"
        if crypto_volume >= 100:
            return "crypto market maker"
        return "mixed strategy trader"

    if total_volume > 0 and confidence >= 0.35:
        return "active trader"

    return "unclassified trader"


def _ensure_wallet_analysis(
    wallet: str,
    *,
    scan_if_missing: bool,
    traders_db_path: str | Path,
    wallet_export_dir: str | Path,
) -> None:
    if load_wallet_report(wallet, db_path=str(traders_db_path)):
        return

    export_path = Path(wallet_export_dir) / f"{wallet}_activity.json"
    if export_path.exists():
        report = build_wallet_forensics_report(export_path, wallet=wallet)
        report_payload = report.to_dict()
        watch_score = calculate_watch_score(
            report.classification,
            report.confidence,
            report.metrics,
            report.signals,
        )
        report_payload["watch_score"] = watch_score
        save_wallet_report(report_payload, watch_score=watch_score, db_path=str(traders_db_path))
        return

    if scan_if_missing:
        scan_wallet(
            wallet,
            traders_db_path=traders_db_path,
            output_dir=wallet_export_dir,
        )


def _dedupe_wallets(wallets: list[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for wallet in wallets:
        normalized = str(wallet or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
        if limit is not None and len(ordered) >= max(0, int(limit)):
            break
    return ordered
