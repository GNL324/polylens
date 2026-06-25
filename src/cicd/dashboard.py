from __future__ import annotations

from pathlib import Path
from typing import Any

from src.cicd.reports import DeploymentReportStore
from src.cicd.runner import CommandRunner


class DeploymentStatus:
    def __init__(self, *, repo_path: str | Path = ".", report_dir: str | Path = "reports/deployments", runner: CommandRunner | None = None) -> None:
        self.repo_path = Path(repo_path)
        self.report_store = DeploymentReportStore(self.repo_path / report_dir)
        self.runner = runner or CommandRunner()

    def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        history = self.report_store.history(limit=limit)
        latest = history[0] if history else None
        failures = [item for item in history if item.get("status") != "success"]
        rollback_history = [item for item in history if (item.get("rollback_recommendation") or {}).get("recommended")]
        return {
            "current_commit": self._git_value(["rev-parse", "HEAD"]),
            "current_branch": self._git_value(["branch", "--show-current"]),
            "deployment_history": history,
            "latest_deployment": latest,
            "health": (latest or {}).get("health_summary") or {"status": "unknown"},
            "failures": failures,
            "rollback_history": rollback_history,
        }

    def _git_value(self, args: list[str]) -> str:
        result = self.runner.run(["git", *args], cwd=self.repo_path, timeout=30)
        return result.stdout.strip() if result.ok else "unknown"


def create_deployment_status_page(*, repo_path: str | Path = ".") -> None:
    from nicegui import ui

    @ui.page("/deployments")
    def deployments_page() -> None:
        status = DeploymentStatus(repo_path=repo_path).snapshot()
        ui.add_head_html(
            """
            <style>
            body { background: #f6f7f9; color: #1f2933; }
            .deploy-shell { max-width: 1280px; margin: 0 auto; padding: 18px; }
            .deploy-card { background: #fff; border: 1px solid #d9dee7; border-radius: 8px; padding: 14px; }
            .deploy-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
            .deploy-label { color: #52606d; font-size: 12px; text-transform: uppercase; }
            .deploy-value { font-size: 18px; font-weight: 650; }
            </style>
            """
        )
        with ui.column().classes("deploy-shell w-full gap-4"):
            ui.label("Deployment Status").classes("text-2xl font-semibold")
            with ui.row().classes("deploy-grid w-full"):
                _metric("Current branch", status["current_branch"])
                _metric("Current commit", str(status["current_commit"])[:12])
                _metric("Health", status["health"].get("status", "unknown"))
                _metric("Failures", len(status["failures"]))
            latest = status.get("latest_deployment") or {}
            with ui.element("section").classes("deploy-card"):
                ui.label("Latest Deployment").classes("text-lg font-semibold")
                ui.json_editor({"content": {"json": latest}}).props("readonly")
            with ui.element("section").classes("deploy-card"):
                ui.label("Deployment History").classes("text-lg font-semibold")
                ui.json_editor({"content": {"json": status["deployment_history"]}}).props("readonly")
            with ui.element("section").classes("deploy-card"):
                ui.label("Rollback History").classes("text-lg font-semibold")
                ui.json_editor({"content": {"json": status["rollback_history"]}}).props("readonly")


def _metric(label: str, value: Any) -> None:
    from nicegui import ui

    with ui.element("div").classes("deploy-card"):
        ui.label(label).classes("deploy-label")
        ui.label(str(value)).classes("deploy-value")
