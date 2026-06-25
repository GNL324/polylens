from __future__ import annotations

import os
from typing import Any


def deployment_buttons(report_url: str | None = None) -> list[list[dict[str, str]]]:
    mission_control_url = os.environ.get("POLYLENS_MISSION_CONTROL_URL", "https://dash.noelgrca.com/mission-control")
    grafana_url = os.environ.get("POLYLENS_GRAFANA_URL", "https://dash.noelgrca.com/grafana")
    report_base = os.environ.get("POLYLENS_DEPLOYMENT_REPORT_URL", "")
    final_report_url = report_url or report_base
    buttons = [
        [{"text": "Mission Control", "url": mission_control_url}],
        [{"text": "Grafana", "url": grafana_url}],
    ]
    if final_report_url:
        buttons.append([{"text": "Deployment Report", "url": final_report_url}])
    return buttons


def format_deployment_success(report: dict[str, Any]) -> str:
    return _format_deployment("Deployment succeeded", report)


def format_deployment_failure(report: dict[str, Any]) -> str:
    return _format_deployment("Deployment failed", report)


def _format_deployment(title: str, report: dict[str, Any]) -> str:
    health = report.get("health_summary") or {}
    restarted = report.get("services_restarted") or report.get("planned_services") or []
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []
    lines = [
        f"Polylens {title}",
        f"Branch: {report.get('branch', 'unknown')}",
        f"Commit: {str(report.get('commit') or 'unknown')[:12]}",
        f"Author: {report.get('author', 'unknown')}",
        f"Health: {health.get('status', 'unknown')}",
        f"Services: {', '.join(str(unit) for unit in restarted) if restarted else 'none'}",
        f"Warnings: {', '.join(str(item) for item in warnings[:3]) if warnings else 'none'}",
        f"Errors: {', '.join(str(item) for item in errors[:3]) if errors else 'none'}",
    ]
    rollback = report.get("rollback_recommendation") or {}
    if rollback.get("recommended"):
        lines.append("Rollback: recommended after validation")
    return "\n".join(lines)
