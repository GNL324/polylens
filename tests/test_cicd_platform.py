from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.cicd.brain import BrainDeploymentArchiver
from src.cicd.dashboard import DeploymentStatus
from src.cicd.detector import GitDeploymentDetector
from src.cicd.reports import DeploymentReportStore, build_deployment_report
from src.cicd.rollback import RollbackPlanner
from src.cicd.runner import CommandResult
from src.cicd.service_map import ServiceMapper
from src.cicd.smoke import SmokeCheck, SmokeRunner
from src.cicd.telegram import deployment_buttons, format_deployment_failure, format_deployment_success
from src.integrations.telegram_notifications import TelegramNotificationConfig, TelegramNotificationService


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def run(self, command, *, cwd=None, timeout=120):  # type: ignore[no-untyped-def]
        key = tuple(command)
        self.calls.append(list(command))
        return self.responses.get(key, CommandResult(list(command), 0, '{"status": "ok"}', ""))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def test_deployment_detector_reads_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.test")
    _git(repo, "config", "user.name", "CI")
    (repo / "src").mkdir()
    (repo / "src" / "dashboard.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    previous = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "dashboard.py").write_text("new\n", encoding="utf-8")
    _git(repo, "commit", "-am", "dashboard")
    current = _git(repo, "rev-parse", "HEAD")

    detector = GitDeploymentDetector(repo_path=repo)

    assert detector.changed_files(previous, current) == ["src/dashboard.py"]


def test_service_mapping_targets_only_affected_services() -> None:
    mapper = ServiceMapper()
    services = mapper.affected_services(["src/integrations/telegram_console.py", "src/web/dashboard.py"])
    keys = {str(item["key"]) for item in services}

    assert keys == {"telegram", "dashboard"}
    assert "wallet-autonomy.service" not in mapper.affected_units(["src/web/dashboard.py"])
    assert "polylens-dashboard.service" in mapper.affected_units(["src/web/dashboard.py"])


def test_smoke_runner_returns_structured_results() -> None:
    check = SmokeCheck("unit", ("python", "-m", "src.cli", "wallet-service-health", "--json"), ("wallet-service-health",))
    runner = FakeRunner()

    result = SmokeRunner(repo_path=".", runner=runner, checks=(check,)).run(tags=["wallet-service-health"])

    assert result["ok"] is True
    assert result["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert result["checks"][0]["json"] == {"status": "ok"}


def test_deployment_report_store_writes_json_and_markdown(tmp_path: Path) -> None:
    report = build_deployment_report(
        detection={
            "current_commit": "abc123",
            "previous_commit": "def456",
            "author": "Noel",
            "branch": "main",
            "remote": "origin",
            "files_changed": ["src/web/dashboard.py"],
            "affected_units": ["polylens-dashboard.service"],
        },
        deployment={"ok": True, "services_restarted": ["polylens-dashboard.service"], "duration_seconds": 1.2},
        smoke={"ok": True, "checks": [{"name": "dashboard-health", "ok": True, "duration_seconds": 0.1}]},
    )
    files = DeploymentReportStore(tmp_path).save(report)

    assert Path(files["json"]).exists()
    assert Path(files["markdown"]).exists()
    assert json.loads(Path(files["json"]).read_text(encoding="utf-8"))["status"] == "success"
    assert "Deployment Report" in Path(files["markdown"]).read_text(encoding="utf-8")


def test_telegram_deployment_notifications_use_buttons(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    config = TelegramNotificationConfig(bot_token="token", chat_id="chat", audit_db_path=str(tmp_path / "telegram.db"))
    service = TelegramNotificationService(
        config,
        request_sender=lambda method, params, token: calls.append(params) or {"ok": True},
    )
    report = {"status": "success", "branch": "main", "commit": "abcdef123456", "author": "Noel", "health_summary": {"status": "healthy"}}

    result = service.send_deployment_success(report, report_url="https://example.test/report")

    assert result["sent"] is True
    assert "Deployment succeeded" in calls[0]["text"]
    markup = json.loads(str(calls[0]["reply_markup"]))
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert labels == ["Mission Control", "Grafana", "Deployment Report"]
    assert "Deployment failed" in format_deployment_failure({"commit": "bad", "health_summary": {"status": "unhealthy"}})
    assert "Deployment succeeded" in format_deployment_success(report)
    assert deployment_buttons("https://example.test/report")[-1][0]["text"] == "Deployment Report"


def test_rollback_planner_requires_explicit_enablement() -> None:
    plan = RollbackPlanner().plan(
        previous_commit="prev",
        failed_commit="fail",
        services=["polylens-dashboard.service", "polylens-dashboard.service"],
    )
    validation = RollbackPlanner().validate(plan)

    assert validation["ok"] is True
    assert plan.enabled is False
    assert plan.services == ("polylens-dashboard.service",)
    assert plan.commands()[1] == "git reset --hard prev"


def test_brain_archival_is_idempotent(tmp_path: Path) -> None:
    report_path = tmp_path / "deployment.json"
    report_path.write_text('{"commit": "abc", "status": "success"}', encoding="utf-8")
    archiver = BrainDeploymentArchiver(manifest_path=tmp_path / "manifest.json", ingest_command="")

    first = archiver.archive(report_path, {"commit": "abc", "status": "success"})
    second = archiver.archive(report_path, {"commit": "abc", "status": "success"})

    assert first["archived"] is True
    assert second["duplicate"] is True


def test_dashboard_status_snapshot_reads_history(tmp_path: Path) -> None:
    reports = tmp_path / "reports" / "deployments"
    reports.mkdir(parents=True)
    (reports / "one.json").write_text(
        json.dumps({"status": "failed", "commit": "abc", "health_summary": {"status": "unhealthy"}, "rollback_recommendation": {"recommended": True}}),
        encoding="utf-8",
    )
    runner = FakeRunner(
        {
            ("git", "rev-parse", "HEAD"): CommandResult(["git", "rev-parse", "HEAD"], 0, "abc\n", ""),
            ("git", "branch", "--show-current"): CommandResult(["git", "branch", "--show-current"], 0, "main\n", ""),
        }
    )

    snapshot = DeploymentStatus(repo_path=tmp_path, report_dir="reports/deployments", runner=runner).snapshot()

    assert snapshot["current_commit"] == "abc"
    assert snapshot["current_branch"] == "main"
    assert snapshot["health"]["status"] == "unhealthy"
    assert len(snapshot["failures"]) == 1
    assert len(snapshot["rollback_history"]) == 1
