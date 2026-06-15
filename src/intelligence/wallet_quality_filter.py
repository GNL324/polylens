from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.intelligence.wallet_synthetic_filter import is_synthetic_seed_wallet, synthetic_wallet_reason

QUALITY_STATUSES = ("accepted", "rejected", "probation")


@dataclass
class WalletQualityScore:
    wallet: str
    quality_score: float
    confidence: float
    status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class WalletQualityFilter:
    """Filter inactive, duplicate, low-confidence, malformed, and stale wallets."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.25,
        min_activity: int = 1,
        min_quality_score: float = 35.0,
        stale_days: int = 120,
    ) -> None:
        self.min_confidence = min_confidence
        self.min_activity = min_activity
        self.min_quality_score = min_quality_score
        self.stale_days = stale_days

    def _is_stale(self, metadata: dict[str, Any]) -> bool:
        nested = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
        last_seen = metadata.get("last_seen") or nested.get("last_seen")
        if not last_seen:
            return False
        try:
            seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - seen).total_seconds() / 86_400.0
            return age_days > self.stale_days
        except ValueError:
            return False

    def score_wallet(
        self,
        wallet: str,
        metadata: dict[str, Any],
        *,
        valid: bool = True,
        reason: str | None = None,
    ) -> WalletQualityScore:
        if not valid:
            return WalletQualityScore(
                wallet=wallet,
                quality_score=0.0,
                confidence=0.0,
                status="rejected",
                reason=reason or "malformed wallet",
            )

        if is_synthetic_seed_wallet(wallet, metadata=metadata):
            return WalletQualityScore(
                wallet=wallet,
                quality_score=0.0,
                confidence=0.0,
                status="rejected",
                reason=f"synthetic wallet: {synthetic_wallet_reason(wallet, metadata=metadata)}",
            )

        confidence = float(metadata.get("confidence") or 0.0)
        activity = int(metadata.get("activity_level") or metadata.get("evidence_count") or 0)
        market_participation = int(metadata.get("market_participation") or 0)
        discovery_score = int(metadata.get("discovery_score") or 0)

        if confidence < self.min_confidence and activity < self.min_activity:
            return WalletQualityScore(
                wallet=wallet,
                quality_score=round(_clamp(confidence * 100), 2),
                confidence=confidence,
                status="rejected",
                reason="low confidence and inactive",
            )

        if self._is_stale(metadata):
            return WalletQualityScore(
                wallet=wallet,
                quality_score=20.0,
                confidence=confidence,
                status="rejected",
                reason="stale wallet",
            )

        quality = _clamp(
            confidence * 40.0
            + min(activity, 50) * 0.8
            + min(market_participation, 20) * 1.5
            + min(discovery_score, 100) * 0.2
        )

        if quality < self.min_quality_score and confidence < self.min_confidence:
            return WalletQualityScore(
                wallet=wallet,
                quality_score=round(quality, 2),
                confidence=confidence,
                status="rejected",
                reason="low quality score",
            )

        if quality < self.min_quality_score + 15:
            return WalletQualityScore(
                wallet=wallet,
                quality_score=round(quality, 2),
                confidence=confidence,
                status="probation",
                reason="developing quality",
            )

        return WalletQualityScore(
            wallet=wallet,
            quality_score=round(quality, 2),
            confidence=confidence,
            status="accepted",
            reason="meets acquisition thresholds",
        )
