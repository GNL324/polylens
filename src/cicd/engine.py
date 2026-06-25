from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.cicd.brain import BrainDeploymentArchiver
from src.cicd.reports import DeploymentReportStore, build_deployment_report
from src.cicd.rollback import RollbackPlanner
from src.cicd.runner import CommandRunner
from src.cicd.smoke import SmokeRunner


@dataclass(frozen=True)
class DeploymentEngineConfig:
    runtime_host: str = "noel@192.168.68.62"
    runtime_repo: str = "/home/noel/polylens"
    report_dir: str = "reports/deployments"
    execute: bool = False
    rollback_enabled: bool = False


class DeploymentEngine:
    def __init__(
        self,
        config: DeploymentEngineConfig | None = None,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config or DeploymentEngineConfig()
        self.runner = runner or CommandRunner()

    def deploy(self, detection: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        units = [str(unit) for unit in detection.get("affected_units") or []]
        deployment = self._deployment_plan(detection, units)
        rollback_plan = RollbackPlanner().plan(
            previous_commit=str(detection.get("previous_commit") or ""),
            failed_commit=str(detection.get("current_commit") or ""),
            services=units,
            enabled=self.config.rollback_enabled,
        )
        smoke: dict[str, Any] = {"ok": True, "status": "skipped", "checks": [], "summary": {"total": 0, "passed": 0, "failed": 0}}
        if self.config.execute:
            deployment = self._execute_deployment(detection, units)
            smoke = self._run_remote_smoke(detection, units)
        else:
            deployment["warnings"].append("dry-run only; pass --execute to mutate Predix")
        deployment["duration_seconds"] = round(time.monotonic() - started, 3)
        report = build_deployment_report(
            detection=detection,
            deployment=deployment,
            smoke=smoke,
            rollback=rollback_plan.to_dict(),
        )
        store = DeploymentReportStore(Path(self.config.report_dir))
        files = store.save(report)
        brain = BrainDeploymentArchiver(manifest_path=Path(self.config.report_dir) / "brain_archive_manifest.json")
        brain_result = brain.archive(files["json"], report)
        return {"report": report, "files": files, "brain": brain_result}

    def _deployment_plan(self, detection: dict[str, Any], units: list[str]) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "dry_run",
            "runtime_host": self.config.runtime_host,
            "runtime_repo": self.config.runtime_repo,
            "commands": [
                "git fetch --all --prune",
                "git pull --ff-only",
                *[f"systemctl restart {unit}" for unit in units],
            ],
            "services_restarted": [],
            "planned_services": units,
            "warnings": [],
            "errors": [],
            "target_commit": detection.get("current_commit"),
        }

    def _execute_deployment(self, detection: dict[str, Any], units: list[str]) -> dict[str, Any]:
        commands = [
            "git fetch --all --prune",
            "git pull --ff-only",
            *[f"systemctl restart {unit}" for unit in units],
        ]
        results = [self._remote(command).to_dict() for command in commands]
        ok = all(bool(item.get("ok")) for item in results)
        return {
            "ok": ok,
            "mode": "execute",
            "runtime_host": self.config.runtime_host,
            "runtime_repo": self.config.runtime_repo,
            "commands": commands,
            "command_results": results,
            "services_restarted": units if ok else [],
            "warnings": [],
            "errors": [str(item.get("stderr") or item.get("stdout")) for item in results if not item.get("ok")],
            "target_commit": detection.get("current_commit"),
        }

    def _run_remote_smoke(self, detection: dict[str, Any], units: list[str]) -> dict[str, Any]:
        command = (
            "python - <<'PY'\n"
            "import json\n"
            "from src.cicd.smoke import SmokeRunner\n"
            f"print(json.dumps(SmokeRunner(repo_path='.').run(tags={detection.get('smoke_tags') or []!r}, systemd_units={units!r}), sort_keys=True))\n"
            "PY"
        )
        result = self._remote(command, timeout=600)
        if result.ok:
            try:
                return dict(__import__("json").loads(result.stdout))
            except Exception:
                pass
        return {
            "ok": False,
            "status": "failed",
            "checks": [
                {
                    "name": "remote-smoke-runner",
                    "ok": False,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }
            ],
            "summary": {"total": 1, "passed": 0, "failed": 1},
        }

    def _remote(self, command: str, *, timeout: int = 300):
        remote_command = f"cd {self.config.runtime_repo} && {command}"
        return self.runner.run(["ssh", self.config.runtime_host, remote_command], timeout=timeout)


def run_local_smoke(repo_path: str | Path = ".", tags: list[str] | None = None, units: list[str] | None = None) -> dict[str, Any]:
    return SmokeRunner(repo_path=repo_path).run(tags=tags, systemd_units=units)
