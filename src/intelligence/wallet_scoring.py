from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.paper_copy_trader import DEFAULT_PAPER_COPY_DB, paper_copy_report
from src.analysis.trader_alpha import build_trader_alpha_report
from src.analysis.trader_discovery import DEFAULT_TRADER_DISCOVERY_DB, load_discovered_wallets
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, load_wallet_report
from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB
from src.intelligence.strategy_classifier import StrategyClassifier
from src.intelligence.wallet_signal_analytics import _wallet_validation_stats
from src.sqlite_utils import closing_connection

LIFECYCLE_CATEGORIES = ("elite", "strong", "developing", "weak", "unscored")
DEFAULT_TRADERS_DB_PATH = DEFAULT_TRADERS_DB


@dataclass
class WalletScore:
    wallet: str
    score: float
    confidence: float
    rank: int
    category: str
    components: dict[str, float]
    metrics: dict[str, Any]

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


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _latest_activity_timestamp(wallet: str, wallet_activity_db_path: str | Path) -> float | None:
    path = Path(wallet_activity_db_path)
    if not path.exists():
        return None
    with closing_connection(path) as conn:
        row = conn.execute(
            "SELECT MAX(timestamp) AS latest FROM wallet_events WHERE wallet = ?",
            (wallet.lower(),),
        ).fetchone()
    if row is None or row["latest"] is None:
        return None
    return float(row["latest"])


def _paper_wallet_stats(wallet: str, paper_copy_db_path: str | Path) -> dict[str, Any]:
    from src.analysis.paper_copy_trader import paper_copy_report_by_wallet
    return paper_copy_report_by_wallet(wallet, db_path=paper_copy_db_path)


class WalletScorer:
    """Unified wallet scoring using registry, alpha, paper outcomes, and signal validation."""

    def __init__(
        self,
        *,
        traders_db_path: str | Path = DEFAULT_TRADERS_DB,
        discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
        signal_db_path: str | Path = DEFAULT_TRADER_SIGNAL_DB,
        paper_copy_db_path: str | Path = DEFAULT_PAPER_COPY_DB,
        wallet_activity_db_path: str | Path = "data/wallet_activity.db",
    ) -> None:
        self.traders_db_path = Path(traders_db_path)
        self.discovery_db_path = Path(discovery_db_path)
        self.signal_db_path = Path(signal_db_path)
        self.paper_copy_db_path = Path(paper_copy_db_path)
        self.wallet_activity_db_path = Path(wallet_activity_db_path)
        self._classifier = StrategyClassifier(traders_db_path=self.traders_db_path)

    def _category_from_score(self, score: float) -> str:
        if score >= 80:
            return "elite"
        if score >= 65:
            return "strong"
        if score >= 45:
            return "developing"
        if score > 0:
            return "weak"
        return "unscored"

    def score_wallet(self, wallet: str) -> WalletScore:
        wallet = str(wallet or "").strip().lower()
        alpha = build_trader_alpha_report(
            wallet,
            traders_db_path=self.traders_db_path,
            discovery_db_path=self.discovery_db_path,
        )
        record = load_wallet_report(wallet, db_path=str(self.traders_db_path))
        metrics = {}
        if record:
            metrics = record.to_dict().get("report", {}).get("metrics", {})

        paper = _paper_wallet_stats(wallet, self.paper_copy_db_path)
        validations = _wallet_validation_stats(self.signal_db_path).get(wallet, {})
        closed_positions = int(paper.get("closed_positions") or 0)
        win_rate = 0.0
        if closed_positions > 0:
            path = Path(self.paper_copy_db_path)
            if path.exists():
                with closing_connection(path) as conn:
                    wins = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM paper_copy_positions
                            WHERE source_wallet = ? AND status = 'closed' AND COALESCE(pnl, 0) > 0
                            """,
                            (wallet,),
                        ).fetchone()[0]
                    )
                win_rate = wins / closed_positions

        roi = _safe_float(paper.get("roi"))
        signal_accuracy = _safe_float(validations.get("accuracy"))
        signal_roi_proxy = _safe_float(validations.get("avg_roi_proxy"))
        realized_roi = roi if closed_positions >= 3 else signal_roi_proxy

        activity_count = alpha.activity_count
        markets_traded = alpha.markets_traded
        latest_ts = _latest_activity_timestamp(wallet, self.wallet_activity_db_path)
        freshness = 0.0
        if latest_ts:
            age_days = max(0.0, (datetime.now(timezone.utc).timestamp() - latest_ts) / 86_400.0)
            freshness = _clamp(100.0 - age_days * 2.0)

        roi_score = _clamp(realized_roi * 100.0 + 50.0) if realized_roi else alpha.alpha_score * 0.5
        expectancy = _clamp((realized_roi + signal_roi_proxy) * 50.0 + 50.0) if (realized_roi or signal_roi_proxy) else alpha.alpha_score * 0.4
        consistency = _clamp(signal_accuracy * 100.0) if validations.get("validation_count", 0) >= 5 else alpha.confidence * 50.0
        win_rate_score = _clamp(win_rate * 100.0) if closed_positions >= 3 else consistency * 0.6
        drawdown_penalty = _clamp(abs(min(0.0, realized_roi)) * 100.0)
        drawdown_score = _clamp(100.0 - drawdown_penalty)
        activity_score = _clamp(activity_count / 2.0 + markets_traded)
        diversity_score = _clamp(markets_traded * 2.5)

        components = {
            "roi": round(roi_score, 4),
            "expectancy": round(expectancy, 4),
            "consistency": round(consistency, 4),
            "win_rate": round(win_rate_score, 4),
            "drawdown": round(drawdown_score, 4),
            "activity_frequency": round(activity_score, 4),
            "market_diversity": round(diversity_score, 4),
            "signal_freshness": round(freshness, 4),
            "alpha": round(float(alpha.alpha_score), 4),
            "watch": round(float(alpha.watch_score), 4),
        }

        composite = (
            components["roi"] * 0.18
            + components["expectancy"] * 0.12
            + components["consistency"] * 0.12
            + components["win_rate"] * 0.15
            + components["drawdown"] * 0.08
            + components["activity_frequency"] * 0.10
            + components["market_diversity"] * 0.10
            + components["signal_freshness"] * 0.10
            + components["alpha"] * 0.05
        )
        composite = round(_clamp(composite), 4)

        sample_size = closed_positions + int(validations.get("validation_count") or 0) + activity_count
        confidence = round(_clamp(math.log1p(sample_size) / math.log1p(100) * alpha.confidence), 4)

        archetype = "unknown"
        profile = self._classifier.load_profile(wallet)
        if profile:
            archetype = profile.archetype
        elif record:
            archetype = str(record.to_dict().get("report", {}).get("classification") or "unknown")

        return WalletScore(
            wallet=wallet,
            score=composite,
            confidence=confidence,
            rank=0,
            category=self._category_from_score(composite) if archetype == "unknown" else archetype,
            components=components,
            metrics={
                "closed_positions": closed_positions,
                "win_rate": round(win_rate, 4) if closed_positions else None,
                "realized_roi": round(roi, 4) if closed_positions else None,
                "signal_accuracy": signal_accuracy or None,
                "activity_count": activity_count,
                "markets_traded": markets_traded,
                "alpha_score": alpha.alpha_score,
                "watch_score": alpha.watch_score,
                "archetype": archetype,
            },
        )

    def rank_wallets(
        self,
        wallets: list[str],
        *,
        performance_boosts: dict[str, float] | None = None,
    ) -> list[WalletScore]:
        scored = [self.score_wallet(wallet) for wallet in wallets]
        if performance_boosts:
            adjusted: list[WalletScore] = []
            for row in scored:
                boost = performance_boosts.get(row.wallet, 0.0)
                adjusted_score = round(_clamp(row.score + boost * 100.0), 4)
                adjusted.append(
                    WalletScore(
                        wallet=row.wallet,
                        score=adjusted_score,
                        confidence=row.confidence,
                        rank=row.rank,
                        category=row.category,
                        components={**row.components, "performance_boost": round(boost * 100.0, 4)},
                        metrics=row.metrics,
                    )
                )
            scored = adjusted
        scored.sort(key=lambda row: (-row.score, -row.confidence, row.wallet))
        ranked: list[WalletScore] = []
        for index, row in enumerate(scored, start=1):
            ranked.append(
                WalletScore(
                    wallet=row.wallet,
                    score=row.score,
                    confidence=row.confidence,
                    rank=index,
                    category=row.category,
                    components=row.components,
                    metrics=row.metrics,
                )
            )
        return ranked
