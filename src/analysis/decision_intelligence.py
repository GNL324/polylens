from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from src.analysis.opportunity_scoring_v3 import PortfolioContext, load_portfolio_context, score_opportunity_v3
from src.analysis.paper_execution_simulator import ExecutionResult, PaperExecutionConfig, simulate_entry
from src.analysis.paper_trading_engine import (
    DEFAULT_PAPER_TRADING_DB,
    PaperOpportunity,
    PaperTradingConfig,
    calculate_position_size,
    init_paper_trading_db,
)
from src.analysis.paper_trading_engine import _entry_block_reason as portfolio_entry_block_reason
from src.analysis.paper_trading_engine import _normalize_opportunity as normalize_paper_opportunity

DecisionType = Literal["BUY", "WATCH", "BLOCK"]

POSITIVE_LABELS: dict[str, str] = {
    "wallet_performance": "Strong wallet performance",
    "wallet_consistency": "Consistent wallet track record",
    "wallet_momentum": "Positive wallet momentum",
    "signal_family_accuracy": "High signal-family accuracy",
    "market_liquidity": "Good liquidity",
    "bid_ask_spread": "Narrow spread",
    "time_to_expiry": "Favorable time to expiry",
    "validating_wallets": "Strong validating-wallet consensus",
    "consensus_strength": "Strong consensus backing",
    "contrarian_bonus": "Contrarian edge detected",
    "similar_market_edge": "Strong historical edge",
}

PENALTY_LABELS: dict[str, str] = {
    "execution_quality_penalty": "Execution quality concerns",
    "portfolio_exposure_penalty": "Portfolio concentration",
}

EXECUTION_BLOCK_LABELS: dict[str, str] = {
    "stale_market_data": "Market stale",
    "market_closed": "Market closed",
    "market_expired": "Market expired",
    "liquidity_cap_exceeded": "Liquidity cap exceeded",
    "invalid_fill_price": "Invalid fill price",
    "partial_fill_liquidity_cap": "Partial fill only",
}

PORTFOLIO_BLOCK_LABELS: dict[str, str] = {
    "insufficient paper cash or strategy capacity": "Kelly sizing below minimum",
    "max concurrent position limit reached": "Max concurrent positions reached",
    "max exposure per market exceeded": "Portfolio exposure limit",
    "max exposure per category exceeded": "Category exposure limit",
    "daily trade limit reached": "Daily trade limit reached",
    "daily loss limit reached": "Daily loss limit reached",
}

MIN_BUY_SCORE = 55.0
MIN_WATCH_SCORE = 40.0
MIN_BUY_CONFIDENCE = 0.45


def explain_opportunity(
    opportunity: dict[str, Any],
    *,
    scoring_v3: dict[str, Any] | None = None,
    execution: ExecutionResult | dict[str, Any] | None = None,
    portfolio: PortfolioContext | dict[str, Any] | None = None,
    validation_history: dict[str, Any] | None = None,
    paper_config: PaperTradingConfig | None = None,
    db_path: str | Path | None = None,
    requested_stake: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Produce a deterministic explanation object for one paper opportunity."""
    now = _as_utc(now or datetime.now(timezone.utc))
    paper_config = paper_config or PaperTradingConfig.from_env()
    execution_config = paper_config.resolved_execution()
    if execution_config.now is None:
        execution_config = PaperExecutionConfig(
            max_market_data_age_seconds=execution_config.max_market_data_age_seconds,
            default_spread=execution_config.default_spread,
            default_slippage_bps=execution_config.default_slippage_bps,
            liquidity_participation_cap=execution_config.liquidity_participation_cap,
            allow_partial_fills=execution_config.allow_partial_fills,
            now=now,
        )

    portfolio_ctx = _portfolio_context(portfolio, db_path)
    raw = opportunity.get("raw") if isinstance(opportunity.get("raw"), dict) else opportunity
    scoring_input = _scoring_input(opportunity)
    if portfolio is not None or portfolio_ctx.open_positions:
        scoring = score_opportunity_v3(scoring_input, portfolio=portfolio_ctx, now=now)
    else:
        scoring = scoring_v3 or opportunity.get("scoring_v3") or score_opportunity_v3(
            scoring_input,
            portfolio=portfolio_ctx,
            now=now,
        )
    components = scoring.get("components") or {}
    rank = opportunity.get("rank") or scoring.get("rank")
    opportunity_id = str(
        opportunity.get("opportunity_id")
        or scoring.get("opportunity_id")
        or raw.get("opportunity_id")
        or raw.get("id")
        or ""
    )
    title = str(opportunity.get("title") or raw.get("title") or raw.get("market_title") or "unknown")
    side = str(opportunity.get("side") or raw.get("side") or "yes")
    paper_opportunity = normalize_paper_opportunity({**raw, **opportunity, "opportunity_id": opportunity_id})

    stake = requested_stake
    if stake is None and db_path is not None:
        init_paper_trading_db(db_path)
        stake = calculate_position_size(
            db_path=db_path,
            strategy=paper_opportunity.strategy,
            config=paper_config,
            opportunity=paper_opportunity,
        )
    elif stake is None:
        equity = max(portfolio_ctx.starting_bankroll, portfolio_ctx.available_cash)
        fraction = paper_config.percent_of_equity or paper_config.risk_per_trade
        stake = round(equity * max(0.0, float(fraction)), 6)

    execution_result = _execution_result(execution, raw=raw, side=side, stake=stake, config=execution_config)
    portfolio_block = None
    if db_path is not None:
        init_paper_trading_db(db_path)
        portfolio_block = portfolio_entry_block_reason(db_path, paper_opportunity, stake, paper_config)

    positives = _positive_contributors(components)
    risks = _risk_contributors(components, raw=raw, validation_history=validation_history)
    confidence_reducers = _confidence_reducers(scoring, components, validation_history, raw)
    blocking_reasons = _blocking_reasons(
        execution_result=execution_result,
        portfolio_block=portfolio_block,
        stake=stake,
        components=components,
        portfolio=portfolio_ctx,
        paper_config=paper_config,
    )
    execution_prevented = _execution_prevented(execution_result, blocking_reasons)
    decision, decision_reasons = _resolve_decision(
        scoring=scoring,
        blocking_reasons=blocking_reasons,
        execution_result=execution_result,
        stake=stake,
        risks=risks,
    )
    improvement_suggestions = _improvement_suggestions(
        decision=decision,
        blocking_reasons=blocking_reasons,
        risks=risks,
        execution_result=execution_result,
        raw=raw,
        validation_history=validation_history,
    )
    summary = _summary_sentence(
        rank=rank,
        scoring=scoring,
        decision=decision,
        positives=positives,
        risks=risks,
        blocking_reasons=blocking_reasons,
    )

    return {
        "opportunity_id": opportunity_id,
        "rank": rank,
        "title": title,
        "strategy": paper_opportunity.strategy,
        "market_id": paper_opportunity.market_id,
        "summary": summary,
        "positives": positives,
        "risks": risks,
        "decision": decision,
        "decision_reasons": decision_reasons,
        "blocking_reasons": blocking_reasons,
        "confidence_reducers": confidence_reducers,
        "execution_prevented": execution_prevented,
        "improvement_suggestions": improvement_suggestions,
        "score": scoring.get("final_score"),
        "confidence": scoring.get("confidence"),
        "expected_edge": scoring.get("expected_edge"),
        "requested_stake": stake,
        "execution_status": execution_result.status,
        "execution_reason": execution_result.reason,
        "read_only": True,
        "paper_only": True,
    }


def explain_opportunities(
    opportunities: list[dict[str, Any]],
    *,
    portfolio: PortfolioContext | dict[str, Any] | None = None,
    validation_history: dict[str, Any] | None = None,
    paper_config: PaperTradingConfig | None = None,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    return [
        explain_opportunity(
            item,
            scoring_v3=item.get("scoring_v3"),
            portfolio=portfolio,
            validation_history=validation_history,
            paper_config=paper_config,
            db_path=db_path,
            now=now,
        )
        for item in opportunities
    ]


def build_decision_intelligence_report(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    opportunity_id: str | None = None,
    limit: int = 10,
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    from src.analysis.opportunity_feed import get_paper_trading_opportunities
    from src.analysis.outcome_validator import outcome_validation_report

    now = now or datetime.now(timezone.utc)
    init_paper_trading_db(db_path)
    validation_history = outcome_validation_report(db_path, window_days=window_days, now=now)
    portfolio = load_portfolio_context(db_path)
    paper_config = PaperTradingConfig.from_env()

    if opportunity_id:
        opportunities = get_paper_trading_opportunities(
            limit=None,
            portfolio_db_path=db_path,
            include_auxiliary=False,
            now=now,
        )
        matches = [row for row in opportunities if str(row.get("opportunity_id") or row.get("id") or "") == opportunity_id]
        if not matches:
            return {
                "timestamp": now.isoformat().replace("+00:00", "Z"),
                "paper_only": True,
                "read_only": True,
                "opportunity_id": opportunity_id,
                "found": False,
                "explanations": [],
            }
        explanations = explain_opportunities(
            matches[:1],
            portfolio=portfolio,
            validation_history=validation_history,
            paper_config=paper_config,
            db_path=db_path,
            now=now,
        )
    else:
        opportunities = get_paper_trading_opportunities(
            limit=limit,
            portfolio_db_path=db_path,
            include_auxiliary=False,
            now=now,
        )
        explanations = explain_opportunities(
            opportunities,
            portfolio=portfolio,
            validation_history=validation_history,
            paper_config=paper_config,
            db_path=db_path,
            now=now,
        )

    return {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "paper_only": True,
        "read_only": True,
        "count": len(explanations),
        "explanations": explanations,
    }


def format_decision_intelligence_telegram(report: dict[str, Any], *, limit: int = 8) -> str:
    lines = ["🧠 Decision Intelligence", ""]
    explanations = report.get("explanations") or []
    if not explanations:
        lines.append("No paper opportunities available for decision review.")
        return "\n".join(lines)
    for item in explanations[:limit]:
        rank = item.get("rank") or "?"
        decision = item.get("decision") or "WATCH"
        icon = {"BUY": "✅", "WATCH": "👀", "BLOCK": "⛔"}.get(decision, "•")
        lines.append(f"{icon} #{rank} {decision} | score={_float(item.get('score')):.1f}")
        lines.append(f"   {str(item.get('title') or 'unknown')[:72]}")
        lines.append(f"   {item.get('summary') or ''}")
        lines.append("")
    lines.append("Tap a rank button below for full drill-down.")
    return "\n".join(lines).strip()


def format_decision_detail_telegram(explanation: dict[str, Any]) -> str:
    lines = [
        "🧠 Decision Intelligence",
        "",
        f"#{explanation.get('rank') or '?'} {explanation.get('decision')} — {explanation.get('title')}",
        "",
        explanation.get("summary") or "",
        "",
        f"Score: {_float(explanation.get('score')):.1f}/100",
        f"Confidence: {_float(explanation.get('confidence')):.0%}",
        f"Edge: {_float(explanation.get('expected_edge')):.2%}",
        "",
        "Positives:",
    ]
    positives = explanation.get("positives") or []
    lines.extend([f"• {item}" for item in positives[:5]] or ["• None highlighted"])
    lines.extend(["", "Risks:"])
    risks = explanation.get("risks") or []
    lines.extend([f"• {item}" for item in risks[:5]] or ["• None flagged"])
    if explanation.get("confidence_reducers"):
        lines.extend(["", "Confidence reducers:"])
        lines.extend([f"• {item}" for item in explanation["confidence_reducers"][:4]])
    lines.extend(["", "Decision reasons:"])
    lines.extend([f"• {item}" for item in (explanation.get("decision_reasons") or [])[:4]])
    if explanation.get("blocking_reasons"):
        lines.extend(["", "Blocking reasons:"])
        lines.extend([f"• {item}" for item in explanation["blocking_reasons"][:5]])
    if explanation.get("execution_prevented"):
        lines.extend(["", "Execution prevented by:"])
        lines.extend([f"• {item}" for item in explanation["execution_prevented"][:4]])
    if explanation.get("improvement_suggestions"):
        lines.extend(["", "Improvement suggestions:"])
        lines.extend([f"• {item}" for item in explanation["improvement_suggestions"][:4]])
    return "\n".join(lines)


def decision_detail_callback(rank: int) -> str:
    return f"di_r{int(rank):02d}"


def parse_decision_detail_callback(callback_id: str) -> int | None:
    if not callback_id.startswith("di_r"):
        return None
    try:
        return int(callback_id[4:])
    except ValueError:
        return None


def _portfolio_context(portfolio: PortfolioContext | dict[str, Any] | None, db_path: str | Path | None) -> PortfolioContext:
    if isinstance(portfolio, PortfolioContext):
        return portfolio
    if isinstance(portfolio, dict):
        return PortfolioContext(
            open_positions=list(portfolio.get("open_positions") or []),
            starting_bankroll=_float(portfolio.get("starting_bankroll"), 100.0),
            available_cash=_float(portfolio.get("available_cash") or portfolio.get("cash_balance"), 100.0),
            daily_risk_used=_float(portfolio.get("daily_risk_used") or portfolio.get("exposure"), 0.0),
            daily_risk_budget=_float(portfolio.get("daily_risk_budget"), 10.0),
        )
    if db_path is not None:
        return load_portfolio_context(db_path)
    return PortfolioContext()


def _scoring_input(opportunity: dict[str, Any]) -> dict[str, Any]:
    stale = {
        "components",
        "final_score",
        "ranking_score",
        "scoring_v3",
        "positive_total",
        "penalty_total",
        "expected_edge",
        "confidence",
        "expected_execution_quality",
        "projected_risk",
        "explanation_summary",
        "read_only",
        "paper_only",
    }
    return {key: value for key, value in opportunity.items() if key not in stale}


def _execution_result(
    execution: ExecutionResult | dict[str, Any] | None,
    *,
    raw: dict[str, Any],
    side: str,
    stake: float,
    config: PaperExecutionConfig,
) -> ExecutionResult:
    if isinstance(execution, ExecutionResult):
        return execution
    if isinstance(execution, dict):
        return ExecutionResult(
            status=str(execution.get("status") or "rejected"),
            fill_price=execution.get("fill_price"),
            filled_stake=_float(execution.get("filled_stake")),
            requested_stake=_float(execution.get("requested_stake"), stake),
            shares=_float(execution.get("shares")),
            slippage=_float(execution.get("slippage")),
            spread_paid=_float(execution.get("spread_paid")),
            quoted_mid=execution.get("quoted_mid"),
            reason=execution.get("reason"),
            metadata_notes=list(execution.get("metadata_notes") or []),
        )
    embedded = raw.get("execution")
    if isinstance(embedded, dict):
        return _execution_result(embedded, raw=raw, side=side, stake=stake, config=config)
    return simulate_entry(row=raw, side=side, requested_stake=stake, config=config)


def _positive_contributors(components: dict[str, dict[str, Any]]) -> list[str]:
    ranked = sorted(
        ((name, data) for name, data in components.items() if _float(data.get("weighted_contribution")) > 0),
        key=lambda item: _float(item[1].get("weighted_contribution")),
        reverse=True,
    )
    labels: list[str] = []
    for name, data in ranked[:5]:
        label = POSITIVE_LABELS.get(name, name.replace("_", " ").title())
        reason = str(data.get("reason") or "").strip()
        if reason and label not in labels:
            labels.append(label if name not in {"market_liquidity", "bid_ask_spread"} else f"{label} ({reason})")
        elif label not in labels:
            labels.append(label)
    return labels


def _risk_contributors(
    components: dict[str, dict[str, Any]],
    *,
    raw: dict[str, Any],
    validation_history: dict[str, Any] | None,
) -> list[str]:
    risks: list[str] = []
    penalties = sorted(
        ((name, data) for name, data in components.items() if _float(data.get("weighted_contribution")) < 0),
        key=lambda item: _float(item[1].get("weighted_contribution")),
    )
    for name, data in penalties[:4]:
        label = PENALTY_LABELS.get(name, name.replace("_", " ").title())
        reason = str(data.get("reason") or "")
        if "wide spread" in reason.lower():
            risks.append("Wide spread")
        elif "liquidity" in reason.lower():
            risks.append("Low liquidity")
        elif "correlated" in reason.lower() or "exposed" in reason.lower() or "exposure" in reason.lower():
            risks.append("Correlated exposure" if "correlated" in reason.lower() else "Portfolio exposure")
        elif "daily risk" in reason.lower():
            risks.append("Daily risk elevated")
        elif label not in risks:
            risks.append(label)

    signal_family = str(raw.get("signal_family") or raw.get("signal_type") or "")
    if validation_history:
        family_stats = (validation_history.get("by_signal_family") or {}).get(signal_family) or {}
        accuracy = _float(family_stats.get("accuracy"))
        if accuracy and accuracy < 0.5:
            risks.append("Weak historical edge")
        drift = _float(validation_history.get("calibration_drift"))
        if drift > 0.08:
            risks.append("Low confidence calibration")

    spread = _float(raw.get("spread"))
    if spread > 0.08 and "Wide spread" not in risks:
        risks.append("Wide spread")
    liquidity = _float(raw.get("liquidity") or raw.get("volume"))
    if 0 < liquidity < 25 and "Low liquidity" not in risks:
        risks.append("Low liquidity")

    return risks[:6]


def _confidence_reducers(
    scoring: dict[str, Any],
    components: dict[str, dict[str, Any]],
    validation_history: dict[str, Any] | None,
    raw: dict[str, Any],
) -> list[str]:
    reducers: list[str] = []
    confidence = _float(scoring.get("confidence"))
    if confidence < MIN_BUY_CONFIDENCE:
        reducers.append(f"Confidence below buy threshold ({confidence:.0%})")
    accuracy_score = _float((components.get("signal_family_accuracy") or {}).get("score"))
    if accuracy_score < 50:
        reducers.append("Weak signal-family accuracy history")
    validation_count = int(_float(raw.get("validation_count") or raw.get("validating_wallets")))
    if validation_count < 3:
        reducers.append("Limited validating-wallet sample")
    if validation_history:
        drift = _float(validation_history.get("calibration_drift"))
        if drift > 0.05:
            reducers.append(f"Calibration drift ({drift:.1%})")
        bias = _float(validation_history.get("confidence_bias"))
        if abs(bias) > 0.05:
            reducers.append(f"Confidence bias ({bias:+.1%})")
    execution_penalty = _float((components.get("execution_quality_penalty") or {}).get("score"))
    if execution_penalty >= 20:
        reducers.append("Execution quality penalty applied")
    return reducers[:5]


def _blocking_reasons(
    *,
    execution_result: ExecutionResult,
    portfolio_block: str | None,
    stake: float,
    components: dict[str, dict[str, Any]],
    portfolio: PortfolioContext,
    paper_config: PaperTradingConfig,
) -> list[str]:
    reasons: list[str] = []
    if execution_result.status == "rejected":
        reason = str(execution_result.reason or "execution_rejected")
        reasons.append(EXECUTION_BLOCK_LABELS.get(reason, "Execution rejected"))
    if portfolio_block:
        reasons.append(PORTFOLIO_BLOCK_LABELS.get(portfolio_block, portfolio_block))
    if stake <= 0:
        label = PORTFOLIO_BLOCK_LABELS["insufficient paper cash or strategy capacity"]
        if label not in reasons:
            reasons.append(label)
    exposure_reason = str((components.get("portfolio_exposure_penalty") or {}).get("reason") or "")
    if portfolio.daily_risk_budget > 0:
        utilization = portfolio.daily_risk_used / portfolio.daily_risk_budget
        if utilization >= 1.0:
            reasons.append("Daily risk exceeded")
        elif utilization >= 0.9 and "Daily risk exceeded" not in reasons:
            reasons.append("Daily risk nearly exhausted")
    if "daily risk budget nearly exhausted" in exposure_reason.lower():
        if "Daily risk exceeded" not in reasons and "Daily risk nearly exhausted" not in reasons:
            reasons.append("Daily risk nearly exhausted")
    if paper_config.daily_loss_limit is not None and portfolio_block == "daily loss limit reached":
        reasons.append("Daily loss limit reached")
    return _dedupe(reasons)


def _execution_prevented(execution_result: ExecutionResult, blocking_reasons: list[str]) -> list[str]:
    prevented: list[str] = []
    if execution_result.status == "rejected":
        reason = str(execution_result.reason or "")
        prevented.append(EXECUTION_BLOCK_LABELS.get(reason, "Execution rejected"))
    for item in blocking_reasons:
        if item in {
            "Market stale",
            "Market closed",
            "Market expired",
            "Execution rejected",
            "Liquidity cap exceeded",
            "Kelly sizing below minimum",
            "Portfolio exposure limit",
            "Daily risk exceeded",
        } and item not in prevented:
            prevented.append(item)
    return prevented


def _resolve_decision(
    *,
    scoring: dict[str, Any],
    blocking_reasons: list[str],
    execution_result: ExecutionResult,
    stake: float,
    risks: list[str],
) -> tuple[DecisionType, list[str]]:
    score = _float(scoring.get("final_score"))
    confidence = _float(scoring.get("confidence"))
    reasons: list[str] = []

    hard_blocks = {
        "Market stale",
        "Market closed",
        "Market expired",
        "Execution rejected",
        "Liquidity cap exceeded",
        "Kelly sizing below minimum",
        "Daily risk exceeded",
        "Max concurrent positions reached",
        "Daily loss limit reached",
    }
    if any(item in hard_blocks for item in blocking_reasons):
        reasons.extend(blocking_reasons[:3])
        return "BLOCK", reasons

    if blocking_reasons:
        reasons.extend(blocking_reasons[:3])
        return "BLOCK", reasons

    if execution_result.status == "partial":
        reasons.append("Only partial fill available under liquidity cap")
        return "WATCH", reasons

    if score >= MIN_BUY_SCORE and confidence >= MIN_BUY_CONFIDENCE and len(risks) <= 2:
        reasons.append(f"Score {score:.1f} and confidence {confidence:.0%} meet buy thresholds")
        if execution_result.status == "filled":
            reasons.append("Execution simulation filled cleanly")
        return "BUY", reasons

    if score >= MIN_WATCH_SCORE:
        if risks:
            reasons.append(f"Score {score:.1f} is actionable but risks remain: {', '.join(risks[:2])}")
        else:
            reasons.append(f"Score {score:.1f} is borderline; waiting for stronger confirmation")
        return "WATCH", reasons

    reasons.append(f"Score {score:.1f} below watch threshold")
    if risks:
        reasons.append(f"Risk profile: {', '.join(risks[:2])}")
    return "BLOCK", reasons


def _improvement_suggestions(
    *,
    decision: DecisionType,
    blocking_reasons: list[str],
    risks: list[str],
    execution_result: ExecutionResult,
    raw: dict[str, Any],
    validation_history: dict[str, Any] | None,
) -> list[str]:
    suggestions: list[str] = []
    if "Wide spread" in risks or "Market stale" in blocking_reasons:
        suggestions.append("Wait for tighter spread.")
    if "Market stale" in blocking_reasons:
        suggestions.append("Re-score after new market data.")
    if any("exposure" in item.lower() or "Portfolio" in item for item in blocking_reasons + risks):
        suggestions.append("Reduce exposure first.")
    validation_count = int(_float(raw.get("validation_count")))
    if validation_count < 3:
        suggestions.append("Wait for additional validating wallets.")
    if execution_result.reason == "partial_fill_liquidity_cap":
        suggestions.append("Reduce stake size or wait for deeper liquidity.")
    if decision == "WATCH" and not suggestions:
        suggestions.append("Re-score after new market data.")
    if decision == "BLOCK" and not suggestions:
        suggestions.append("Wait for stronger wallet and liquidity confirmation.")
    return _dedupe(suggestions)[:5]


def _summary_sentence(
    *,
    rank: Any,
    scoring: dict[str, Any],
    decision: DecisionType,
    positives: list[str],
    risks: list[str],
    blocking_reasons: list[str],
) -> str:
    rank_text = f"Ranked #{rank}" if rank else "Ranked opportunity"
    lead = positives[0].lower() if positives else "balanced scoring profile"
    if risks:
        tail = f"but reduced by {risks[0].lower()}"
    elif blocking_reasons:
        tail = f"but blocked by {blocking_reasons[0].lower()}"
    else:
        tail = f"decision: {decision.lower()}"
    score = _float(scoring.get("final_score"))
    return f"{rank_text} (score {score:.1f}) because of {lead}, {tail}."


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
