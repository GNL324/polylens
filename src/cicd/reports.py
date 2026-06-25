from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_deployment_report(
    *,
    detection: dict[str, Any],
    deployment: dict[str, Any],
    smoke: dict[str, Any],
    rollback: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    affected_units = detection.get("affected_units") or []
    status = "success" if deployment.get("ok") and smoke.get("ok") else "failed"
    return {
        "schema": "polylens.deployment_report.v1",
        "generated_at": utc_now(),
        "status": status,
        "commit": detection.get("current_commit"),
        "previous_commit": detection.get("previous_commit"),
        "author": detection.get("author"),
        "branch": detection.get("branch"),
        "remote": detection.get("remote"),
        "message": detection.get("message"),
        "files_changed": detection.get("files_changed") or [],
        "affected_services": detection.get("affected_services") or [],
        "services_restarted": deployment.get("services_restarted") or [],
        "planned_services": affected_units,
        "duration_seconds": deployment.get("duration_seconds"),
        "smoke_results": smoke,
        "health_summary": summarize_health(smoke),
        "warnings": warnings or deployment.get("warnings") or [],
        "errors": errors or deployment.get("errors") or [],
        "rollback_recommendation": rollback or rollback_recommendation(status, affected_units),
        "deployment": deployment,
    }


def summarize_health(smoke: dict[str, Any]) -> dict[str, Any]:
    checks = smoke.get("checks") or []
    failed = [item for item in checks if not item.get("ok")]
    return {
        "status": "healthy" if smoke.get("ok") else "unhealthy",
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "failed_checks": [item.get("name") for item in failed],
    }


def rollback_recommendation(status: str, services: list[str] | tuple[str, ...]) -> dict[str, Any]:
    if status == "success":
        return {"recommended": False, "reason": "deployment and smoke validation passed", "services": list(services)}
    return {
        "recommended": True,
        "reason": "deployment or smoke validation failed; validate rollback plan before execution",
        "services": list(services),
    }


class DeploymentReportStore:
    def __init__(self, report_dir: str | Path = "reports/deployments") -> None:
        self.report_dir = Path(report_dir)

    def save(self, report: dict[str, Any]) -> dict[str, str]:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        commit = str(report.get("commit") or "unknown")[:12]
        timestamp = str(report.get("generated_at") or utc_now()).replace(":", "").replace("+", "Z")
        stem = f"{timestamp}_{commit}"
        json_path = self.report_dir / f"{stem}.json"
        md_path = self.report_dir / f"{stem}.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
        md_path.write_text(render_markdown_report(report), encoding="utf-8")
        return {"json": str(json_path), "markdown": str(md_path)}

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for path in sorted(self.report_dir.glob("*.json"), reverse=True):
            if path.name == "brain_archive_manifest.json":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            payload["_path"] = str(path)
            reports.append(payload)
            if len(reports) >= limit:
                break
        return reports


def render_markdown_report(report: dict[str, Any]) -> str:
    health = report.get("health_summary") or {}
    lines = [
        f"# Deployment Report: {report.get('status', 'unknown')}",
        "",
        f"- Commit: `{report.get('commit')}`",
        f"- Previous commit: `{report.get('previous_commit')}`",
        f"- Branch: `{report.get('branch')}`",
        f"- Author: {report.get('author')}",
        f"- Duration: {report.get('duration_seconds')}s",
        f"- Health: {health.get('status', 'unknown')}",
        "",
        "## Files Changed",
        *[f"- `{path}`" for path in report.get("files_changed", [])],
        "",
        "## Services Restarted",
        *[f"- `{unit}`" for unit in report.get("services_restarted", [])],
        "",
        "## Smoke Results",
    ]
    for check in (report.get("smoke_results") or {}).get("checks", []):
        status = "PASS" if check.get("ok") else "FAIL"
        lines.append(f"- {status}: `{check.get('name')}` ({check.get('duration_seconds')}s)")
    warnings = report.get("warnings") or []
    errors = report.get("errors") or []
    if warnings:
        lines.extend(["", "## Warnings", *[f"- {item}" for item in warnings]])
    if errors:
        lines.extend(["", "## Errors", *[f"- {item}" for item in errors]])
    lines.extend(["", "## Rollback Recommendation", json.dumps(report.get("rollback_recommendation") or {}, indent=2, sort_keys=True)])
    return "\n".join(lines) + "\n"
