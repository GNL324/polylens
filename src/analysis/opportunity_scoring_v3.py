from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DAILY_RISK_BUDGET_PCT = 0.10
CONSENSUS_SIGNAL_TYPES = frozenset({"consensus", "co_market", "crowd"})
CONTRARIAN_SIGNAL_TYPES = frozenset({"contrarian", "fade", "counter"})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ScoringV3Weights:
    wallet_performance: float = 0.10
    wallet_consistency: float = 0.08
    wallet_momentum: float = 0.07
    signal_family_accuracy: float = 0.10
    market_liquidity: float = 0.10
    bid_ask_spread: float = 0.08
    time_to_expiry: float = 0.07
    validating_wallets: float = 0.06
    consensus_strength: float = 0.05
    contrarian_bonus: float = 0.04
    similar_market_edge: float = 0.10
    execution_quality_penalty: float = 0.08
    portfolio_exposure_penalty: float = 0.07

    @classmethod
    def from_env(cls) -> ScoringV3Weights:
        return cls(
            wallet_performance=_env_float("POLYLENS_SCORE_V3_WEIGHT_WALLET_PERFORMANCE", 0.10),
            wallet_consistency=_env_float("POLYLENS_SCORE_V3_WEIGHT_WALLET_CONSISTENCY", 0.08),
            wallet_momentum=_env_float("POLYLENS_SCORE_V3_WEIGHT_WALLET_MOMENTUM", 0.07),
            signal_family_accuracy=_env_float("POLYLENS_SCORE_V3_WEIGHT_SIGNAL_FAMILY_ACCURACY", 0.10),
            market_liquidity=_env_float("POLYLENS_SCORE_V3_WEIGHT_MARKET_LIQUIDITY", 0.10),
            bid_ask_spread=_env_float("POLYLENS_SCORE_V3_WEIGHT_BID_ASK_SPREAD", 0.08),
            time_to_expiry=_env_float("POLYLENS_SCORE_V3_WEIGHT_TIME_TO_EXPIRY", 0.07),
            validating_wallets=_env_float("POLYLENS_SCORE_V3_WEIGHT_VALIDATING_WALLETS", 0.06),
            consensus_strength=_env_float("POLYLENS_SCORE_V3_WEIGHT_CONSENSUS_STRENGTH", 0.05),
            contrarian_bonus=_env_float("POLYLENS_SCORE_V3_WEIGHT_CONTRARIAN_BONUS", 0.04),
            similar_market_edge=_env_float("POLYLENS_SCORE_V3_WEIGHT_SIMILAR_MARKET_EDGE", 0.10),
            execution_quality_penalty=_env_float("POLYLENS_SCORE_V3_WEIGHT_EXECUTION_QUALITY_PENALTY", 0.08),
            portfolio_exposure_penalty=_env_float("POLYLENS_SCORE_V3_WEIGHT_PORTFOLIO_EXPOSURE_PENALTY", 0.07),
        )


@dataclass
class PortfolioContext:
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    starting_bankroll: float = 100.0
    available_cash: float = 100.0
    daily_risk_used: float = 0.0
    daily_risk_budget: float = 10.0

    @property
    def strategy_exposure(self) -> dict[str, float]:
        buckets: dict[str, float] = {}
        for row in self.open_positions:
            strategy = str(row.get("strategy") or "unknown")
            buckets[strategy] = buckets.get(strategy, 0.0) + _float(row.get("notional"))
        return buckets

    @property
    def market_exposure(self) -> dict[str, float]:
        buckets: dict[str, float] = {}
        for row in self.open_positions:
            market_id = str(row.get("market_id") or row.get("opportunity_id") or "unknown")
            buckets[market_id] = buckets.get(market_id, 0.0) + _float(row.get("notional"))
        return buckets

    @property
    def wallet_exposure(self) -> dict[str, float]:
        buckets: dict[str, float] = {}
        for row in self.open_positions:
            wallet = str(row.get("source_wallet") or row.get("wallet") or "").lower()
            if not wallet:
                continue
            buckets[wallet] = buckets.get(wallet, 0.0) + _float(row.get("notional"))
        return buckets


def load_portfolio_context(db_path: str | Path | None) -> PortfolioContext:
    path = Path(db_path) if db_path else None
    if path is None or not path.exists():
        return PortfolioContext()
    from src.analysis.paper_portfolio import portfolio_report
    from src.analysis.paper_trading_engine import init_paper_trading_db, open_positions_report

    init_paper_trading_db(path)
    portfolio = portfolio_report(path)
    open_rows = open_positions_report(path).get("open_positions", [])
    starting = _float(portfolio.get("starting_bankroll"), 100.0)
    open_notional = sum(_float(row.get("notional")) for row in open_rows)
    return PortfolioContext(
        open_positions=open_rows,
        starting_bankroll=starting,
        available_cash=_float(portfolio.get("cash_balance"), starting),
        daily_risk_used=open_notional,
        daily_risk_budget=starting * DEFAULT_DAILY_RISK_BUDGET_PCT,
    )


def score_opportunity_v3(
    candidate: dict[str, Any],
    *,
    portfolio: PortfolioContext | None = None,
    weights: ScoringV3Weights | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    portfolio = portfolio or PortfolioContext()
    weights = weights or ScoringV3Weights.from_env()
    now = _as_utc(now or datetime.now(timezone.utc))
    row = dict(candidate)
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else row

    components: dict[str, dict[str, Any]] = {}
    positive_total = 0.0
    penalty_total = 0.0

    positive_specs = [
        ("wallet_performance", weights.wallet_performance, _wallet_performance_score(raw)),
        ("wallet_consistency", weights.wallet_consistency, _wallet_consistency_score(raw)),
        ("wallet_momentum", weights.wallet_momentum, _wallet_momentum_score(raw)),
        ("signal_family_accuracy", weights.signal_family_accuracy, _signal_family_accuracy_score(raw)),
        ("market_liquidity", weights.market_liquidity, _liquidity_score(raw)),
        ("bid_ask_spread", weights.bid_ask_spread, _spread_score(raw)),
        ("time_to_expiry", weights.time_to_expiry, _time_to_expiry_score(raw, now=now)),
        ("validating_wallets", weights.validating_wallets, _validating_wallets_score(raw)),
        ("consensus_strength", weights.consensus_strength, _consensus_strength_score(raw)),
        ("contrarian_bonus", weights.contrarian_bonus, _contrarian_bonus_score(raw)),
        ("similar_market_edge", weights.similar_market_edge, _similar_market_edge_score(raw)),
    ]
    for name, weight, payload in positive_specs:
        score = payload["score"]
        contribution = round(score * weight, 6)
        positive_total += contribution
        components[name] = {
            "score": round(score, 4),
            "weight": weight,
            "weighted_contribution": contribution,
            "reason": payload["reason"],
        }

    penalty_specs = [
        ("execution_quality_penalty", weights.execution_quality_penalty, _execution_quality_penalty(raw, now=now)),
        ("portfolio_exposure_penalty", weights.portfolio_exposure_penalty, _portfolio_exposure_penalty(raw, portfolio)),
    ]
    for name, weight, payload in penalty_specs:
        penalty = payload["score"]
        contribution = round(penalty * weight, 6)
        penalty_total += contribution
        components[name] = {
            "score": round(penalty, 4),
            "weight": weight,
            "weighted_contribution": -contribution,
            "reason": payload["reason"],
        }

    final_score = round(_clamp(positive_total - penalty_total, 0.0, 100.0), 4)
    expected_edge = _expected_edge(raw)
    confidence = _confidence(raw, components)
    execution_quality = round(100.0 - components["execution_quality_penalty"]["score"], 4)
    projected_risk = round(
        components["portfolio_exposure_penalty"]["score"] * 0.6
        + (100.0 - components["market_liquidity"]["score"]) * 0.4,
        4,
    )
    explanation = _explanation_summary(final_score, components)

    return {
        "opportunity_id": str(row.get("opportunity_id") or row.get("id") or raw.get("id") or ""),
        "final_score": final_score,
        "ranking_score": final_score,
        "components": components,
        "expected_edge": round(expected_edge, 6),
        "confidence": round(confidence, 6),
        "expected_execution_quality": execution_quality,
        "projected_risk": projected_risk,
        "explanation_summary": explanation,
        "positive_total": round(positive_total, 6),
        "penalty_total": round(penalty_total, 6),
        "read_only": True,
        "paper_only": True,
        **row,
    }


def rank_opportunities_v3(
    candidates: list[dict[str, Any]],
    *,
    portfolio: PortfolioContext | None = None,
    weights: ScoringV3Weights | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    portfolio = portfolio or PortfolioContext()
    scored = [score_opportunity_v3(item, portfolio=portfolio, weights=weights, now=now) for item in candidates]
    scored.sort(
        key=lambda row: (
            -_float(row.get("final_score")),
            -_float(row.get("expected_edge")),
            str(row.get("opportunity_id") or ""),
        )
    )
    ranked: list[dict[str, Any]] = []
    for index, row in enumerate(scored, start=1):
        payload = dict(row)
        payload["rank"] = index
        payload["scoring_v3"] = {
            "final_score": payload["final_score"],
            "rank": index,
            "components": payload["components"],
            "expected_edge": payload["expected_edge"],
            "confidence": payload["confidence"],
            "expected_execution_quality": payload["expected_execution_quality"],
            "projected_risk": payload["projected_risk"],
            "explanation_summary": payload["explanation_summary"],
        }
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
        merged_raw = {**raw, "scoring_v3": payload["scoring_v3"]}
        payload["raw"] = merged_raw
        ranked.append(payload)
    if limit is not None:
        ranked = ranked[: max(int(limit), 0)]
    return ranked


def format_opportunity_ranking_text(opportunities: list[dict[str, Any]], *, limit: int = 10) -> str:
    lines = ["🏆 Opportunity Rankings", ""]
    if not opportunities:
        lines.append("No ranked paper opportunities available.")
        return "\n".join(lines)
    for row in opportunities[:limit]:
        scoring = row.get("scoring_v3") or row
        lines.append(
            f"#{scoring.get('rank', '?')} score={_float(scoring.get('final_score')):.1f} "
            f"edge={_float(scoring.get('expected_edge')):.2%} "
            f"conf={_float(scoring.get('confidence')):.0%}"
        )
        title = str(row.get("title") or row.get("market_title") or "unknown")[:72]
        lines.append(f"   {title}")
        lines.append(f"   {scoring.get('explanation_summary', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def _wallet_performance_score(raw: dict[str, Any]) -> dict[str, Any]:
    alpha = _float(raw.get("alpha_score") or raw.get("watch_score"))
    roi = _float(raw.get("wallet_roi") or raw.get("realized_roi") or raw.get("roi"))
    win_rate = _float(raw.get("wallet_win_rate") or raw.get("win_rate"))
    if alpha <= 0 and roi <= 0 and win_rate <= 0:
        return {"score": 50.0, "reason": "no wallet performance history; neutral default"}
    score = _clamp(alpha * 0.5 + _clamp(roi, -1.0, 1.0) * 50.0 + 50.0 + win_rate * 20.0, 0.0, 100.0)
    return {"score": score, "reason": f"wallet alpha={alpha:.0f}, roi={roi:.2%}, win_rate={win_rate:.0%}"}


def _wallet_consistency_score(raw: dict[str, Any]) -> dict[str, Any]:
    confidence = _float(raw.get("confidence") or raw.get("confidence_score"))
    validation_count = _float(raw.get("validation_count") or raw.get("signal_validation_count"))
    sample_factor = min(1.0, validation_count / 20.0) if validation_count else 0.35
    score = _clamp(confidence * 70.0 + sample_factor * 30.0, 0.0, 100.0)
    return {"score": score, "reason": f"consistency from confidence={confidence:.2f}, validations={int(validation_count)}"}


def _wallet_momentum_score(raw: dict[str, Any]) -> dict[str, Any]:
    trend = str(raw.get("wallet_trend") or raw.get("trend") or "").lower()
    delta = _float(raw.get("score_delta") or raw.get("momentum"))
    if trend == "improving":
        base = 75.0
    elif trend == "declining":
        base = 35.0
    elif trend == "stable":
        base = 55.0
    else:
        base = 50.0
    score = _clamp(base + delta, 0.0, 100.0)
    return {"score": score, "reason": f"wallet momentum trend={trend or 'unknown'}, delta={delta:.2f}"}


def _signal_family_accuracy_score(raw: dict[str, Any]) -> dict[str, Any]:
    accuracy = _float(raw.get("historical_accuracy") or raw.get("signal_accuracy"))
    gate = str(raw.get("gate_status") or "").lower()
    if accuracy <= 0:
        return {"score": 45.0, "reason": "no signal-family accuracy history; conservative default"}
    score = accuracy * 100.0
    if gate == "proven":
        score = min(100.0, score + 8.0)
    elif gate in {"weak", "blocked"}:
        score = max(0.0, score - 20.0)
    return {"score": score, "reason": f"signal family accuracy={accuracy:.1%}, gate={gate or 'unknown'}"}


def _liquidity_score(raw: dict[str, Any]) -> dict[str, Any]:
    liquidity = _float(raw.get("liquidity") or raw.get("liquidity_usd") or raw.get("volume"))
    if liquidity <= 0:
        return {"score": 25.0, "reason": "missing liquidity metadata; conservative low score"}
    score = _clamp(math.log10(liquidity + 1.0) / 5.0 * 100.0, 0.0, 100.0)
    return {"score": score, "reason": f"market liquidity=${liquidity:,.0f}"}


def _spread_score(raw: dict[str, Any]) -> dict[str, Any]:
    spread = _float(raw.get("spread"))
    bid = _float(raw.get("yes_bid") or raw.get("best_bid"))
    ask = _float(raw.get("yes_ask") or raw.get("best_ask"))
    if spread <= 0 and bid > 0 and ask > 0:
        spread = max(0.0, ask - bid)
    if spread <= 0:
        spread = 0.02
        reason = "missing spread; assumed 2c default"
    else:
        reason = f"bid/ask spread={spread:.4f}"
    score = _clamp(100.0 - (spread / 0.20) * 100.0, 0.0, 100.0)
    return {"score": score, "reason": reason}


def _time_to_expiry_score(raw: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    expiry = _parse_timestamp(raw.get("expiry_time") or raw.get("close_time") or raw.get("end_time"))
    if expiry is None:
        return {"score": 55.0, "reason": "no expiry metadata; neutral time score"}
    hours = max(0.0, (expiry - now).total_seconds() / 3600.0)
    if hours < 1:
        score = 15.0
    elif hours < 6:
        score = 40.0
    elif hours < 24:
        score = 65.0
    elif hours < 72:
        score = 80.0
    else:
        score = 90.0
    return {"score": score, "reason": f"time to expiry={hours:.1f}h"}


def _validating_wallets_score(raw: dict[str, Any]) -> dict[str, Any]:
    count = int(_float(raw.get("validation_count") or raw.get("validating_wallets") or raw.get("consensus_count")))
    score = _clamp(min(100.0, count * 8.0), 0.0, 100.0)
    return {"score": score, "reason": f"validating wallets/consensus samples={count}"}


def _consensus_strength_score(raw: dict[str, Any]) -> dict[str, Any]:
    signal_type = str(raw.get("signal_type") or raw.get("signal_family") or "").lower()
    consensus_count = int(_float(raw.get("consensus_count") or raw.get("validation_count")))
    if signal_type in CONSENSUS_SIGNAL_TYPES or consensus_count >= 3:
        score = _clamp(60.0 + consensus_count * 8.0, 0.0, 100.0)
        return {"score": score, "reason": f"consensus signal={signal_type or 'group'} count={consensus_count}"}
    return {"score": 35.0, "reason": "limited consensus backing"}


def _contrarian_bonus_score(raw: dict[str, Any]) -> dict[str, Any]:
    signal_type = str(raw.get("signal_type") or raw.get("signal_family") or "").lower()
    if signal_type in CONTRARIAN_SIGNAL_TYPES:
        edge = _expected_edge(raw)
        score = _clamp(55.0 + edge * 200.0, 0.0, 100.0)
        return {"score": score, "reason": f"contrarian bonus for {signal_type} with edge={edge:.2%}"}
    return {"score": 20.0, "reason": "no contrarian setup"}


def _similar_market_edge_score(raw: dict[str, Any]) -> dict[str, Any]:
    edge = _expected_edge(raw)
    score = _clamp(edge / 0.15 * 100.0, 0.0, 100.0)
    return {"score": score, "reason": f"historical/similar-market edge={edge:.2%}"}


def _execution_quality_penalty(raw: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    penalty = 0.0
    reasons: list[str] = []
    spread = _float(raw.get("spread"))
    if spread <= 0:
        bid = _float(raw.get("yes_bid") or raw.get("best_bid"))
        ask = _float(raw.get("yes_ask") or raw.get("best_ask"))
        if bid > 0 and ask > 0:
            spread = ask - bid
    if spread > 0.08:
        penalty += 35.0
        reasons.append(f"wide spread {spread:.3f}")
    elif spread > 0.04:
        penalty += 15.0
        reasons.append(f"moderate spread {spread:.3f}")

    liquidity = _float(raw.get("liquidity") or raw.get("volume"))
    if liquidity <= 0:
        penalty += 25.0
        reasons.append("missing liquidity")
    elif liquidity < 25:
        penalty += 15.0
        reasons.append("thin liquidity")

    quote_ts = _parse_timestamp(raw.get("quote_timestamp") or raw.get("last_update") or raw.get("timestamp"))
    if quote_ts is not None:
        age = (now - quote_ts).total_seconds()
        if age > 3600:
            penalty += 30.0
            reasons.append("stale quote")
        elif age > 900:
            penalty += 12.0
            reasons.append("aging quote")

    if not reasons:
        reasons.append("clean execution profile")
    return {"score": _clamp(penalty, 0.0, 100.0), "reason": "; ".join(reasons)}


def _portfolio_exposure_penalty(raw: dict[str, Any], portfolio: PortfolioContext) -> dict[str, Any]:
    penalty = 0.0
    reasons: list[str] = []
    strategy = str(raw.get("strategy") or "unknown")
    market_id = str(raw.get("market_id") or raw.get("id") or "")
    wallet = str(raw.get("wallet") or raw.get("source_wallet") or raw.get("trader_address") or "").lower()
    asset = str(raw.get("asset") or "").upper()

    strategy_notional = portfolio.strategy_exposure.get(strategy, 0.0)
    if strategy_notional >= portfolio.starting_bankroll * 0.15:
        penalty += 30.0
        reasons.append(f"strategy {strategy} already heavily exposed")

    market_notional = portfolio.market_exposure.get(market_id, 0.0)
    if market_id and market_notional > 0:
        penalty += 25.0
        reasons.append(f"market {market_id} already held")

    if wallet and portfolio.wallet_exposure.get(wallet, 0.0) > 0:
        penalty += 20.0
        reasons.append(f"wallet {wallet[:10]}... already copied")

    correlated = sum(
        1
        for row in portfolio.open_positions
        if str(row.get("asset") or "").upper() == asset and asset not in {"", "OTHER"}
    )
    if correlated >= 2:
        penalty += 15.0
        reasons.append(f"correlated {asset} exposure")

    if portfolio.daily_risk_budget > 0:
        utilization = portfolio.daily_risk_used / portfolio.daily_risk_budget
        if utilization >= 0.9:
            penalty += 25.0
            reasons.append("daily risk budget nearly exhausted")
        elif utilization >= 0.7:
            penalty += 10.0
            reasons.append("daily risk budget elevated")

    if portfolio.available_cash <= portfolio.starting_bankroll * 0.05:
        penalty += 15.0
        reasons.append("low available cash")

    if not reasons:
        reasons.append("portfolio headroom available")
    return {"score": _clamp(penalty, 0.0, 100.0), "reason": "; ".join(reasons)}


def _expected_edge(raw: dict[str, Any]) -> float:
    return _float(
        raw.get("estimated_edge")
        or raw.get("raw_edge")
        or raw.get("estimated_roi")
        or raw.get("guaranteed_roi")
        or raw.get("edge")
    )


def _confidence(raw: dict[str, Any], components: dict[str, dict[str, Any]]) -> float:
    confidence = _float(raw.get("confidence") or raw.get("confidence_score"))
    if confidence > 0:
        return _clamp(confidence, 0.0, 1.0)
    accuracy = components["signal_family_accuracy"]["score"] / 100.0
    consistency = components["wallet_consistency"]["score"] / 100.0
    return round((accuracy * 0.55 + consistency * 0.45), 6)


def _explanation_summary(final_score: float, components: dict[str, dict[str, Any]]) -> str:
    positive = sorted(
        ((name, data) for name, data in components.items() if data["weighted_contribution"] > 0),
        key=lambda item: item[1]["weighted_contribution"],
        reverse=True,
    )
    penalties = [item for item in components.items() if item[1]["weighted_contribution"] < 0]
    lead = positive[0] if positive else ("score", {"reason": "balanced profile"})
    summary = f"Score {final_score:.1f}/100 led by {lead[0].replace('_', ' ')} ({lead[1]['reason']})"
    if penalties:
        worst = min(penalties, key=lambda item: item[1]["weighted_contribution"])
        summary += f"; penalty: {worst[0].replace('_', ' ')} ({worst[1]['reason']})"
    return summary


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
