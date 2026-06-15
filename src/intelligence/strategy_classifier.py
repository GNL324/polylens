from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, load_wallet_report
from src.analysis.wallet_forensics import build_wallet_forensics_report
from src.sqlite_utils import closing_connection

STRATEGY_ARCHETYPES = (
    "EARLY_MOVER",
    "CONTRA_FADE",
    "NEWS_TRADER",
    "ARB_HUNTER",
    "SIZE_SCALPER",
    "CONVICTION_HOLD",
    "MOMENTUM_RIDER",
)


@dataclass
class StrategyProfile:
    wallet: str
    archetype: str
    confidence: float
    forensics_classification: str
    specialization: str
    alpha_score: int
    watch_score: int
    supporting_metrics: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def init_strategy_profiles_db(db_path: str | Path = DEFAULT_TRADERS_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_profiles (
                wallet TEXT PRIMARY KEY,
                archetype TEXT NOT NULL,
                confidence REAL NOT NULL,
                forensics_classification TEXT NOT NULL,
                specialization TEXT NOT NULL,
                alpha_score INTEGER NOT NULL,
                watch_score INTEGER NOT NULL,
                profile_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_profiles_archetype ON strategy_profiles(archetype);
            CREATE INDEX IF NOT EXISTS idx_strategy_profiles_confidence ON strategy_profiles(confidence DESC);
            """
        )


def _derive_specialization(metrics: dict[str, Any], classification: str) -> str:
    btc = _safe_float(metrics.get("btc_volume"))
    eth = _safe_float(metrics.get("eth_volume"))
    sol = _safe_float(metrics.get("sol_volume"))
    if classification == "arbitrage_trader":
        return "cross-market arbitrage"
    if btc >= max(eth, sol, 1) and btc > 0:
        return "btc specialist"
    if eth >= max(btc, sol, 1) and eth > 0:
        return "eth specialist"
    if sol > 0:
        return "sol specialist"
    if classification == "market_maker":
        return "crypto market maker"
    if classification == "quantitative_directional":
        return "directional trader"
    return "mixed strategy trader"


def _score_archetypes(metrics: dict[str, Any], classification: str, confidence: float) -> tuple[str, float, list[str]]:
    trade_count = int(_safe_float(metrics.get("trade_count")))
    markets_traded = int(_safe_float(metrics.get("markets_traded")))
    merge_count = int(_safe_float(metrics.get("merge_count")))
    redeem_count = int(_safe_float(metrics.get("redeem_count")))
    buy_volume = _safe_float(metrics.get("buy_volume"))
    sell_volume = _safe_float(metrics.get("sell_volume"))
    arbitrage_opportunities = int(_safe_float(metrics.get("arbitrage_opportunities")))
    arbitrage_volume = _safe_float(metrics.get("arbitrage_volume"))
    overlap_ratio = _safe_float(metrics.get("overlap_ratio"))
    avg_sum_price = _safe_float(metrics.get("average_sum_price"))

    scores: dict[str, float] = {archetype: 0.0 for archetype in STRATEGY_ARCHETYPES}
    reasons: dict[str, list[str]] = {archetype: [] for archetype in STRATEGY_ARCHETYPES}

    if markets_traded <= 15 and trade_count >= 5:
        scores["EARLY_MOVER"] += 30
        reasons["EARLY_MOVER"].append("concentrated early market entries")
    if trade_count >= 20 and markets_traded >= 10:
        scores["EARLY_MOVER"] += 10
        reasons["EARLY_MOVER"].append("active across multiple markets")

    if classification in {"quantitative_directional", "mixed"} and buy_volume > sell_volume:
        scores["CONTRA_FADE"] += 20
        reasons["CONTRA_FADE"].append("directional classification with net buying")
    if overlap_ratio < 0.15 and trade_count >= 30:
        scores["CONTRA_FADE"] += 15
        reasons["CONTRA_FADE"].append("low overlap suggests contrarian positioning")

    if markets_traded >= 25 and trade_count >= 40:
        scores["NEWS_TRADER"] += 25
        reasons["NEWS_TRADER"].append("high market breadth with frequent trades")
    if trade_count >= 60:
        scores["NEWS_TRADER"] += 15
        reasons["NEWS_TRADER"].append("elevated trade frequency")

    if classification == "arbitrage_trader" or arbitrage_opportunities >= 3:
        scores["ARB_HUNTER"] += 35
        reasons["ARB_HUNTER"].append("arbitrage classification or opportunities detected")
    if arbitrage_volume >= 100 or (avg_sum_price > 0 and avg_sum_price < 0.98):
        scores["ARB_HUNTER"] += 20
        reasons["ARB_HUNTER"].append("meaningful arbitrage volume or sub-parity pricing")
    if merge_count >= 5:
        scores["ARB_HUNTER"] += 10
        reasons["ARB_HUNTER"].append("inventory merge activity")

    median_trade = buy_volume / max(trade_count, 1)
    if trade_count >= 50 and median_trade < 25:
        scores["SIZE_SCALPER"] += 30
        reasons["SIZE_SCALPER"].append("high trade count with small median size")
    if trade_count >= 100:
        scores["SIZE_SCALPER"] += 15
        reasons["SIZE_SCALPER"].append("very high trade frequency")

    if buy_volume >= 500 and sell_volume < buy_volume * 0.3:
        scores["CONVICTION_HOLD"] += 25
        reasons["CONVICTION_HOLD"].append("large buy volume with limited selling")
    if redeem_count <= 2 and buy_volume >= 200:
        scores["CONVICTION_HOLD"] += 15
        reasons["CONVICTION_HOLD"].append("holding pattern with minimal redeems")

    if classification == "quantitative_directional" and overlap_ratio >= 0.2:
        scores["MOMENTUM_RIDER"] += 20
        reasons["MOMENTUM_RIDER"].append("directional trader with shared-market overlap")
    if buy_volume >= 300 and trade_count >= 15:
        scores["MOMENTUM_RIDER"] += 15
        reasons["MOMENTUM_RIDER"].append("sustained buy-side momentum")

    best_archetype = max(scores, key=lambda key: scores[key])
    best_score = scores[best_archetype]
    if best_score <= 0:
        best_archetype = "MOMENTUM_RIDER" if classification == "quantitative_directional" else "CONVICTION_HOLD"
        best_score = 10.0
        reasons[best_archetype].append("default archetype from forensics classification")

    archetype_confidence = min(0.95, max(0.1, (best_score / 50.0) * confidence))
    return best_archetype, round(archetype_confidence, 4), reasons[best_archetype]


class StrategyClassifier:
    """Map existing forensics and alpha analytics to signal-oriented strategy archetypes."""

    def __init__(self, *, traders_db_path: str | Path = DEFAULT_TRADERS_DB) -> None:
        self.traders_db_path = Path(traders_db_path)

    def classify_wallet(
        self,
        wallet: str,
        *,
        activity_path: str | Path | None = None,
        scan_if_missing: bool = True,
    ) -> StrategyProfile:
        wallet = str(wallet or "").strip().lower()
        record = load_wallet_report(wallet, db_path=str(self.traders_db_path))
        if record:
            report = record.to_dict()["report"]
            metrics = report.get("metrics") or {}
            classification = str(report.get("classification") or "unknown")
            confidence = _safe_float(report.get("confidence"))
        elif activity_path and Path(activity_path).exists():
            forensics = build_wallet_forensics_report(str(activity_path), wallet=wallet)
            metrics = forensics.metrics
            classification = forensics.classification
            confidence = forensics.confidence
        elif scan_if_missing:
            raise ValueError(f"no wallet report found for {wallet}; run wallet scan first")
        else:
            metrics = {}
            classification = "unknown"
            confidence = 0.0

        alpha = build_trader_alpha_report(wallet, traders_db_path=self.traders_db_path)
        archetype, archetype_confidence, reasons = _score_archetypes(metrics, classification, confidence)
        specialization = _derive_specialization(metrics, classification)

        return StrategyProfile(
            wallet=wallet,
            archetype=archetype,
            confidence=archetype_confidence,
            forensics_classification=classification,
            specialization=specialization,
            alpha_score=alpha.alpha_score,
            watch_score=alpha.watch_score,
            supporting_metrics={
                "trade_count": int(_safe_float(metrics.get("trade_count"))),
                "markets_traded": int(_safe_float(metrics.get("markets_traded"))),
                "buy_volume": round(_safe_float(metrics.get("buy_volume")), 2),
                "sell_volume": round(_safe_float(metrics.get("sell_volume")), 2),
                "arbitrage_opportunities": int(_safe_float(metrics.get("arbitrage_opportunities"))),
                "overlap_ratio": round(_safe_float(metrics.get("overlap_ratio")), 4),
                "merge_count": int(_safe_float(metrics.get("merge_count"))),
                "redeem_count": int(_safe_float(metrics.get("redeem_count"))),
            },
            reasons=reasons,
        )

    def classify_wallets(self, wallets: list[str], *, activity_dir: str | Path | None = None) -> list[StrategyProfile]:
        activity_dir = Path(activity_dir) if activity_dir else None
        profiles: list[StrategyProfile] = []
        for wallet in wallets:
            activity_path = None
            if activity_dir is not None:
                candidate = activity_dir / f"{wallet}_activity.json"
                if candidate.exists():
                    activity_path = candidate
            profiles.append(self.classify_wallet(wallet, activity_path=activity_path))
        return profiles

    def persist_profile(self, profile: StrategyProfile) -> None:
        init_strategy_profiles_db(self.traders_db_path)
        now = _utc_now()
        with closing_connection(self.traders_db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strategy_profiles (
                    wallet, archetype, confidence, forensics_classification, specialization,
                    alpha_score, watch_score, profile_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.wallet,
                    profile.archetype,
                    profile.confidence,
                    profile.forensics_classification,
                    profile.specialization,
                    profile.alpha_score,
                    profile.watch_score,
                    json.dumps(profile.to_dict(), sort_keys=True),
                    now,
                ),
            )

    def persist_profiles(self, profiles: list[StrategyProfile]) -> dict[str, Any]:
        for profile in profiles:
            self.persist_profile(profile)
        return {"profiles_persisted": len(profiles), "updated_at": _utc_now()}

    def load_profile(self, wallet: str) -> StrategyProfile | None:
        init_strategy_profiles_db(self.traders_db_path)
        wallet = str(wallet or "").strip().lower()
        with closing_connection(self.traders_db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_json FROM strategy_profiles WHERE wallet = ?
                """,
                (wallet,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["profile_json"]))
        return StrategyProfile(**payload)

    def profiles_by_archetype(self, archetype: str, *, limit: int = 25) -> list[StrategyProfile]:
        init_strategy_profiles_db(self.traders_db_path)
        with closing_connection(self.traders_db_path) as conn:
            rows = conn.execute(
                """
                SELECT profile_json FROM strategy_profiles
                WHERE archetype = ?
                ORDER BY confidence DESC, alpha_score DESC
                LIMIT ?
                """,
                (archetype, max(int(limit), 0)),
            ).fetchall()
        return [StrategyProfile(**json.loads(str(row["profile_json"]))) for row in rows]

    @staticmethod
    def median_confidence(profiles: list[StrategyProfile]) -> float:
        if not profiles:
            return 0.0
        return round(statistics.median(profile.confidence for profile in profiles), 4)
