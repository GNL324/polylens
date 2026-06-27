from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.opportunity_scoring_v3 import ScoringV3Weights
from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB
from src.analysis.signal_weight_optimizer import (
    V3_COMPONENT_NAMES,
    WeightOptimizerConfig,
    load_validation_samples,
    optimize_signal_weights,
    weights_as_dict,
)

DEFAULT_REPORT_PATH = "reports/signal_weight_recommendation.json"


def build_signal_weight_report(
    db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
    *,
    window_days: int = 90,
    current: ScoringV3Weights | None = None,
    config: WeightOptimizerConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    current = current or ScoringV3Weights.from_env()
    config = config or WeightOptimizerConfig.from_env()
    samples = load_validation_samples(db_path, window_days=window_days, now=now)
    optimization = optimize_signal_weights(samples, current=current, config=config)
    recommendations = _recommendation_rows(optimization)

    return {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "paper_only": True,
        "read_only": True,
        "auto_apply": False,
        "dry_run": True,
        "window_days": int(window_days),
        "sample_size": optimization["sample_size"],
        "confidence": optimization["confidence"],
        "last_optimization_run": now.isoformat().replace("+00:00", "Z"),
        "current_weights": optimization["current_weights"],
        "suggested_weights": optimization["suggested_weights"],
        "components": optimization["components"],
        "recommendations": recommendations,
        "strongest_factors": optimization.get("strongest_factors", []),
        "weakest_factors": optimization.get("weakest_factors", []),
        "rationale": optimization["rationale"],
    }


def export_signal_weight_recommendation(
    report: dict[str, Any],
    path: str | Path = DEFAULT_REPORT_PATH,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": report.get("timestamp"),
        "sample_size": report.get("sample_size"),
        "confidence": report.get("confidence"),
        "recommendations": report.get("recommendations"),
        "rationale": report.get("rationale"),
        "auto_apply": False,
        "paper_only": True,
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def mission_control_section(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_weights": report.get("current_weights", {}),
        "suggested_weights": report.get("suggested_weights", {}),
        "confidence": report.get("confidence", 0.0),
        "last_optimization_run": report.get("last_optimization_run"),
        "validation_sample_size": report.get("sample_size", 0),
        "recommendations": report.get("recommendations", []),
        "strongest_factors": report.get("strongest_factors", []),
        "weakest_factors": report.get("weakest_factors", []),
        "rationale": report.get("rationale", ""),
        "auto_apply": False,
        "paper_only": True,
    }


def format_weight_optimization_telegram(report: dict[str, Any]) -> str:
    lines = [
        "⚖️ Weight Optimization",
        "",
        f"Sample: {report.get('sample_size', 0)}",
        f"Confidence: {_pct(report.get('confidence', 0.0))}",
        f"Last run: {report.get('last_optimization_run', 'unknown')}",
        "",
        "Strongest factors:",
    ]
    for name in report.get("strongest_factors", [])[:3]:
        comp = report.get("components", {}).get(name, {})
        lines.append(f"• {name.replace('_', ' ')} ({comp.get('predictive_power', 0):.2f})")
    lines.append("")
    lines.append("Weakest factors:")
    for name in report.get("weakest_factors", [])[:3]:
        comp = report.get("components", {}).get(name, {})
        lines.append(f"• {name.replace('_', ' ')} ({comp.get('predictive_power', 0):.2f})")
    lines.append("")
    lines.append("Suggested changes:")
    for row in report.get("recommendations", [])[:5]:
        if abs(float(row.get("delta", 0))) <= 0.0001:
            continue
        sign = "+" if float(row["delta"]) > 0 else ""
        lines.append(
            f"• {row['component'].replace('_', ' ')}: "
            f"{row['current_weight']:.3f} → {row['suggested_weight']:.3f} "
            f"({sign}{row['delta']:.3f}, conf={_pct(row.get('confidence', 0))})"
        )
    lines.append("")
    lines.append(str(report.get("rationale", "")))
    lines.append("")
    lines.append("Recommendations only — not auto-applied.")
    return "\n".join(lines)


def format_weight_optimization_cli(report: dict[str, Any]) -> str:
    lines = [
        "Signal Weight Optimization (dry-run)",
        f"Sample size: {report.get('sample_size', 0)}",
        f"Confidence: {_pct(report.get('confidence', 0.0))}",
        "",
        f"{'Component':<28} {'Current':>8} {'Suggested':>10} {'Delta':>8} {'Conf':>6}",
        "-" * 66,
    ]
    for row in report.get("recommendations", []):
        lines.append(
            f"{row['component']:<28} {row['current_weight']:>8.3f} "
            f"{row['suggested_weight']:>10.3f} {row['delta']:>+8.3f} {_pct(row.get('confidence', 0)):>6}"
        )
    lines.extend(["", str(report.get("rationale", "")), "", "Auto-apply: disabled (recommendations only)."])
    return "\n".join(lines)


def _recommendation_rows(optimization: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in V3_COMPONENT_NAMES:
        comp = optimization["components"][name]
        rows.append(
            {
                "component": name,
                "current_weight": comp["current_weight"],
                "suggested_weight": comp["suggested_weight"],
                "delta": comp["delta"],
                "confidence": comp["confidence"],
                "sample_size": comp["sample_size"],
                "historical_performance": comp["historical_performance"],
                "predictive_power": comp["predictive_power"],
            }
        )
    rows.sort(key=lambda item: abs(float(item["delta"])), reverse=True)
    return rows


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "0%"
