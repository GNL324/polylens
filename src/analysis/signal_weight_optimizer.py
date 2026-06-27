from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from src.analysis.opportunity_scoring_v3 import ScoringV3Weights
from src.analysis.outcome_validator import _validation_rows, init_outcome_validation_db

V3_COMPONENT_NAMES: tuple[str, ...] = (
    "wallet_performance",
    "wallet_consistency",
    "wallet_momentum",
    "signal_family_accuracy",
    "market_liquidity",
    "bid_ask_spread",
    "time_to_expiry",
    "validating_wallets",
    "consensus_strength",
    "contrarian_bonus",
    "similar_market_edge",
    "execution_quality_penalty",
    "portfolio_exposure_penalty",
)

PENALTY_COMPONENTS = frozenset({"execution_quality_penalty", "portfolio_exposure_penalty"})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class WeightOptimizerConfig:
    min_weight: float = 0.02
    max_weight: float = 0.25
    min_samples: int = 5
    full_confidence_samples: int = 50
    max_swing: float = 0.35
    epsilon: float = 1e-6

    @classmethod
    def from_env(cls) -> WeightOptimizerConfig:
        return cls(
            min_weight=_env_float("POLYLENS_WEIGHT_OPT_MIN", 0.02),
            max_weight=_env_float("POLYLENS_WEIGHT_OPT_MAX", 0.25),
            min_samples=int(_env_float("POLYLENS_WEIGHT_OPT_MIN_SAMPLES", 5)),
            full_confidence_samples=int(_env_float("POLYLENS_WEIGHT_OPT_FULL_CONFIDENCE_SAMPLES", 50)),
            max_swing=_env_float("POLYLENS_WEIGHT_OPT_MAX_SWING", 0.35),
        )


def weights_as_dict(weights: ScoringV3Weights) -> dict[str, float]:
    return {name: round(float(getattr(weights, name)), 6) for name in V3_COMPONENT_NAMES}


def normalize_weights(raw: dict[str, float], *, config: WeightOptimizerConfig) -> dict[str, float]:
    bounded = {name: _clamp(float(raw.get(name, 0.0)), config.min_weight, config.max_weight) for name in V3_COMPONENT_NAMES}
    total = sum(bounded.values())
    if total <= config.epsilon:
        return weights_as_dict(ScoringV3Weights())
    normalized = {name: round(bounded[name] / total, 6) for name in V3_COMPONENT_NAMES}
    drift = round(1.0 - sum(normalized.values()), 6)
    if drift and normalized:
        anchor = max(normalized, key=normalized.get)
        normalized[anchor] = round(normalized[anchor] + drift, 6)
    return normalized


def blend_weights(
    current: dict[str, float],
    target: dict[str, float],
    *,
    blend_factor: float,
    config: WeightOptimizerConfig,
) -> dict[str, float]:
    alpha = _clamp(blend_factor, 0.0, config.max_swing)
    mixed = {
        name: round((1.0 - alpha) * current[name] + alpha * target[name], 6)
        for name in V3_COMPONENT_NAMES
    }
    return normalize_weights(mixed, config=config)


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x <= 0 or den_y <= 0:
        return 0.0
    return num / (den_x * den_y)


def load_validation_samples(
    db_path: str | Path,
    *,
    window_days: int = 90,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    init_outcome_validation_db(db_path)
    now = _as_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=max(1, int(window_days)))
    rows = _validation_rows(db_path)
    samples: list[dict[str, Any]] = []
    for row in rows:
        if row.get("validation_status") not in {"correct", "incorrect", "partial"}:
            continue
        settled = _parse_time(row.get("settlement_timestamp"))
        if settled is not None and settled < cutoff:
            continue
        samples.append(row)
    return samples


def extract_component_features(row: dict[str, Any]) -> dict[str, float]:
    raw_payload = _load_raw_payload(row)
    stored = _stored_component_scores(raw_payload)
    if stored:
        return stored
    return _proxy_component_features(row, raw_payload)


def optimize_signal_weights(
    samples: Sequence[dict[str, Any]],
    *,
    current: ScoringV3Weights | None = None,
    config: WeightOptimizerConfig | None = None,
) -> dict[str, Any]:
    current = current or ScoringV3Weights.from_env()
    config = config or WeightOptimizerConfig.from_env()
    current_weights = weights_as_dict(current)
    n = len(samples)

    if n < config.min_samples:
        return {
            "sample_size": n,
            "confidence": 0.0,
            "current_weights": current_weights,
            "suggested_weights": current_weights,
            "components": _empty_component_analysis(current_weights),
            "rationale": f"insufficient sample size ({n} < {config.min_samples}); keeping current weights",
            "auto_apply": False,
            "paper_only": True,
            "read_only": True,
        }

    outcomes = [_float(row.get("correctness_score")) for row in samples]
    feature_matrix = {name: [] for name in V3_COMPONENT_NAMES}
    for row in samples:
        features = extract_component_features(row)
        for name in V3_COMPONENT_NAMES:
            feature_matrix[name].append(features[name])

    component_stats: dict[str, dict[str, Any]] = {}
    raw_scores: dict[str, float] = {}
    confidences: list[float] = []

    for name in V3_COMPONENT_NAMES:
        values = feature_matrix[name]
        correlation = pearson_correlation(values, outcomes)
        if name in PENALTY_COMPONENTS:
            predictive = max(config.epsilon, abs(correlation))
        else:
            predictive = max(config.epsilon, abs(correlation))
        stability = _stability(values, outcomes)
        sample_confidence = min(1.0, n / config.full_confidence_samples)
        confidence = round(sample_confidence * stability, 6)
        confidences.append(confidence)
        raw_scores[name] = predictive
        component_stats[name] = {
            "predictive_power": round(predictive, 6),
            "correlation": round(correlation, 6),
            "stability": round(stability, 6),
            "confidence": confidence,
            "sample_size": n,
            "current_weight": current_weights[name],
        }

    target = normalize_weights(raw_scores, config=config)
    overall_confidence = round(sum(confidences) / len(confidences), 6) if confidences else 0.0
    suggested = blend_weights(
        current_weights,
        target,
        blend_factor=overall_confidence * config.max_swing,
        config=config,
    )

    components = {}
    for name in V3_COMPONENT_NAMES:
        current_w = current_weights[name]
        suggested_w = suggested[name]
        stats = component_stats[name]
        components[name] = {
            **stats,
            "suggested_weight": suggested_w,
            "delta": round(suggested_w - current_w, 6),
            "historical_performance": stats["correlation"],
        }

    strongest = sorted(
        ((name, components[name]) for name in V3_COMPONENT_NAMES if name not in PENALTY_COMPONENTS),
        key=lambda item: item[1]["predictive_power"],
        reverse=True,
    )[:3]
    weakest = sorted(
        ((name, components[name]) for name in V3_COMPONENT_NAMES if name not in PENALTY_COMPONENTS),
        key=lambda item: item[1]["predictive_power"],
    )[:3]

    return {
        "sample_size": n,
        "confidence": overall_confidence,
        "current_weights": current_weights,
        "suggested_weights": suggested,
        "components": components,
        "strongest_factors": [name for name, _ in strongest],
        "weakest_factors": [name for name, _ in weakest],
        "rationale": (
            f"Analyzed {n} validated outcomes; blended {overall_confidence * config.max_swing:.1%} "
            f"toward correlation-weighted targets (confidence={overall_confidence:.2f})"
        ),
        "auto_apply": False,
        "paper_only": True,
        "read_only": True,
    }


def _stored_component_scores(raw_payload: dict[str, Any]) -> dict[str, float] | None:
    scoring = raw_payload.get("scoring_v3")
    if not isinstance(scoring, dict):
        return None
    components = scoring.get("components")
    if not isinstance(components, dict):
        return None
    extracted: dict[str, float] = {}
    for name in V3_COMPONENT_NAMES:
        payload = components.get(name)
        if isinstance(payload, dict) and payload.get("score") is not None:
            extracted[name] = _clamp(_float(payload.get("score")), 0.0, 100.0)
    if len(extracted) == len(V3_COMPONENT_NAMES):
        return extracted
    return None


def _proxy_component_features(row: dict[str, Any], raw: dict[str, Any]) -> dict[str, float]:
    confidence = _float(row.get("confidence_at_entry") or raw.get("confidence"))
    opportunity_score = _float(row.get("opportunity_score"))
    liquidity = _float(row.get("market_liquidity"))
    execution_quality = _float(row.get("execution_quality"))
    expiry_seconds = _float(row.get("time_to_expiry_seconds"))
    signal_family = str(row.get("signal_family") or raw.get("signal_family") or "").lower()

    wallet_perf = _clamp(confidence * 100.0, 0.0, 100.0) if confidence else 50.0
    wallet_consistency = wallet_perf
    wallet_momentum = 50.0
    signal_family_accuracy = _clamp(opportunity_score, 0.0, 100.0) if opportunity_score else 45.0
    market_liquidity = _clamp(math.log10(liquidity + 1.0) / 5.0 * 100.0, 0.0, 100.0) if liquidity else 25.0
    bid_ask_spread = 55.0
    if expiry_seconds < 3600:
        time_to_expiry = 15.0
    elif expiry_seconds < 86400:
        time_to_expiry = 65.0
    elif expiry_seconds > 0:
        time_to_expiry = 85.0
    else:
        time_to_expiry = 55.0
    validating_wallets = 50.0
    consensus_strength = 60.0 if signal_family in {"consensus", "co_market", "crowd"} else 35.0
    contrarian_bonus = 70.0 if signal_family in {"contrarian", "fade", "counter"} else 20.0
    similar_market_edge = signal_family_accuracy
    execution_penalty = _clamp(100.0 - execution_quality, 0.0, 100.0) if execution_quality else 20.0
    portfolio_penalty = _portfolio_penalty_proxy(row)

    return {
        "wallet_performance": wallet_perf,
        "wallet_consistency": wallet_consistency,
        "wallet_momentum": wallet_momentum,
        "signal_family_accuracy": signal_family_accuracy,
        "market_liquidity": market_liquidity,
        "bid_ask_spread": bid_ask_spread,
        "time_to_expiry": time_to_expiry,
        "validating_wallets": validating_wallets,
        "consensus_strength": consensus_strength,
        "contrarian_bonus": contrarian_bonus,
        "similar_market_edge": similar_market_edge,
        "execution_quality_penalty": execution_penalty,
        "portfolio_exposure_penalty": portfolio_penalty,
    }


def _portfolio_penalty_proxy(row: dict[str, Any]) -> float:
    raw_text = row.get("portfolio_state_json")
    if not raw_text:
        return 10.0
    try:
        state = json.loads(str(raw_text))
    except json.JSONDecodeError:
        return 10.0
    cash = _float(state.get("cash_balance") or state.get("available_cash"))
    equity = _float(state.get("total_equity") or state.get("equity"), default=100.0)
    if equity <= 0:
        return 10.0
    utilization = 1.0 - (cash / equity)
    return _clamp(utilization * 100.0, 0.0, 100.0)


def _stability(values: Sequence[float], outcomes: Sequence[float]) -> float:
    if len(values) < 4:
        return 0.5
    midpoint = len(values) // 2
    first = pearson_correlation(values[:midpoint], outcomes[:midpoint])
    second = pearson_correlation(values[midpoint:], outcomes[midpoint:])
    return _clamp(1.0 - abs(first - second), 0.0, 1.0)


def _empty_component_analysis(current: dict[str, float]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "predictive_power": 0.0,
            "correlation": 0.0,
            "stability": 0.0,
            "confidence": 0.0,
            "sample_size": 0,
            "current_weight": current[name],
            "suggested_weight": current[name],
            "delta": 0.0,
            "historical_performance": 0.0,
        }
        for name in V3_COMPONENT_NAMES
    }


def _load_raw_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw_text = row.get("raw_json")
    if not raw_text:
        return {}
    try:
        payload = json.loads(str(raw_text))
    except json.JSONDecodeError:
        return {}
    inner = payload.get("raw")
    return inner if isinstance(inner, dict) else payload if isinstance(payload, dict) else {}


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
