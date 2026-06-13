from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders, load_wallet_report


@dataclass
class TraderAlphaReport:
    wallet: str
    activity_count: int
    markets_traded: int
    shared_markets: int
    overlap_ratio: float
    merge_count: int
    redeem_count: int
    buy_volume: float
    redeem_volume: float
    asset_breakdown: dict[str, float]
    classification: str
    confidence: float
    watch_score: int
    alpha_score: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trader_alpha_report(
    wallet: str,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> dict[str, Any]:
    return build_trader_alpha_report(wallet, traders_db_path=traders_db_path, discovery_db_path=discovery_db_path).to_dict()


def build_trader_alpha_report(
    wallet: str,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> TraderAlphaReport:
    wallet = str(wallet or "").strip().lower()
    record = load_wallet_report(wallet, db_path=str(traders_db_path))
    report = record.to_dict()["report"] if record else {"wallet": wallet, "classification": "unknown", "confidence": 0.0, "metrics": {}, "signals": []}
    metrics = report.get("metrics") or {}
    discovery = _discovery_by_wallet(discovery_db_path).get(wallet)

    classification = str(report.get("classification") or "unknown")
    confidence = _safe_float(report.get("confidence"))
    watch_score = int(_safe_float(report.get("watch_score")))
    if watch_score <= 0 and record:
        # Older stored reports may omit watch_score while wallet table has it.
        for trader in list_traders(limit=10_000, db_path=str(traders_db_path)):
            if trader.wallet == wallet:
                watch_score = trader.watch_score
                break

    activity_count = int(
        _safe_float(metrics.get("trade_count"))
        + _safe_float(metrics.get("merge_count"))
        + _safe_float(metrics.get("redeem_count"))
    )
    shared_markets = max(
        int(_safe_float(metrics.get("overlap_count"))),
        int(discovery.evidence_count) if discovery else 0,
    )
    asset_breakdown = {
        "BTC": round(_safe_float(metrics.get("btc_volume")), 2),
        "ETH": round(_safe_float(metrics.get("eth_volume")), 2),
        "SOL": round(_safe_float(metrics.get("sol_volume")), 2),
        "OTHER": round(_safe_float(metrics.get("other_volume")), 2),
    }
    alpha_score = calculate_alpha_score(
        activity_count=activity_count,
        markets_traded=int(_safe_float(metrics.get("markets_traded"))),
        shared_markets=shared_markets,
        overlap_ratio=_safe_float(metrics.get("overlap_ratio")),
        merge_count=int(_safe_float(metrics.get("merge_count"))),
        redeem_count=int(_safe_float(metrics.get("redeem_count"))),
        buy_volume=_safe_float(metrics.get("buy_volume")),
        redeem_volume=_safe_float(metrics.get("redeem_volume")),
        asset_breakdown=asset_breakdown,
        classification=classification,
        confidence=confidence,
        watch_score=watch_score,
    )
    return TraderAlphaReport(
        wallet=wallet,
        activity_count=activity_count,
        markets_traded=int(_safe_float(metrics.get("markets_traded"))),
        shared_markets=shared_markets,
        overlap_ratio=round(_safe_float(metrics.get("overlap_ratio")), 4),
        merge_count=int(_safe_float(metrics.get("merge_count"))),
        redeem_count=int(_safe_float(metrics.get("redeem_count"))),
        buy_volume=round(_safe_float(metrics.get("buy_volume")), 2),
        redeem_volume=round(_safe_float(metrics.get("redeem_volume")), 2),
        asset_breakdown=asset_breakdown,
        classification=classification,
        confidence=confidence,
        watch_score=watch_score,
        alpha_score=alpha_score,
    )


def rank_trader_alpha(
    limit: int = 25,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> list[dict[str, Any]]:
    traders = list_traders(limit=max(int(limit or 25), 1), db_path=str(traders_db_path))
    reports = [
        build_trader_alpha_report(trader.wallet, traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
        for trader in traders
    ]
    reports.sort(key=lambda item: (-item.alpha_score, -item.watch_score, item.wallet))
    return [report.to_dict() for report in reports[: max(int(limit or 25), 0)]]


def calculate_alpha_score(
    *,
    activity_count: int,
    markets_traded: int,
    shared_markets: int,
    overlap_ratio: float,
    merge_count: int,
    redeem_count: int,
    buy_volume: float,
    redeem_volume: float,
    asset_breakdown: dict[str, float],
    classification: str,
    confidence: float,
    watch_score: int,
) -> int:
    score = 0.0
    score += min(25.0, max(0.0, watch_score) * 0.25)
    score += min(20.0, max(0.0, confidence) * 20.0)
    score += {"market_maker": 12.0, "arbitrage_trader": 12.0, "mixed": 10.0, "quantitative_directional": 7.0}.get(classification, 0.0)
    score += min(12.0, shared_markets * 2.0)
    score += min(10.0, (merge_count + redeem_count) * 1.5)
    score += min(8.0, max(0.0, overlap_ratio) * 16.0)
    score += min(6.0, activity_count / 20.0)
    score += min(5.0, markets_traded / 8.0)
    score += min(5.0, (buy_volume + redeem_volume) / 1000.0)
    score += _specialization_bonus(asset_breakdown)

    if activity_count < 5:
        score -= 20
    elif activity_count < 20:
        score -= 8
    if classification == "unknown":
        score -= 20
    if confidence < 0.35:
        score -= 12
    if buy_volume + redeem_volume < 25:
        score -= 6
    if shared_markets <= 0:
        score -= 8

    return int(round(min(100.0, max(0.0, score))))


def _specialization_bonus(asset_breakdown: dict[str, float]) -> float:
    total = sum(max(0.0, _safe_float(value)) for value in asset_breakdown.values())
    if total <= 0:
        return 0.0
    top_share = max(max(0.0, _safe_float(value)) for value in asset_breakdown.values()) / total
    if top_share >= 0.8:
        return 5.0
    if top_share >= 0.6:
        return 3.0
    return 0.0


def _discovery_by_wallet(discovery_db_path: str | Path) -> dict[str, Any]:
    try:
        return {candidate.wallet: candidate for candidate in load_discovered_wallets(discovery_db_path)}
    except Exception:
        return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
