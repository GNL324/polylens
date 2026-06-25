from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.cicd.runner import CommandRunner
from src.cicd.service_map import ServiceMapper


@dataclass(frozen=True)
class GitDeploymentDetector:
    repo_path: str | Path = "."
    remote: str = "origin"
    branch: str = "main"
    state_path: str | Path = "reports/deployments/state.json"
    runner: CommandRunner | None = None
    mapper: ServiceMapper | None = None

    def detect(self, *, fetch: bool = True) -> dict[str, Any]:
        runner = self.runner or CommandRunner()
        repo = Path(self.repo_path)
        if fetch:
            runner.run(["git", "fetch", self.remote, self.branch, "--prune"], cwd=repo, timeout=180)
        target_ref = f"refs/remotes/{self.remote}/{self.branch}"
        current_commit = self._git(["rev-parse", target_ref], runner).strip()
        previous_commit = self._previous_commit()
        if not previous_commit:
            previous_commit = self._git(["rev-parse", f"{current_commit}^"], runner, allow_failure=True).strip() or current_commit
        changed_files = self.changed_files(previous_commit, current_commit, runner)
        author = self._git(["show", "-s", "--format=%an <%ae>", current_commit], runner).strip()
        message = self._git(["show", "-s", "--format=%s", current_commit], runner).strip()
        mapper = self.mapper or ServiceMapper()
        services = mapper.affected_services(changed_files)
        return {
            "branch": self.branch,
            "remote": self.remote,
            "previous_commit": previous_commit,
            "current_commit": current_commit,
            "new_commits": previous_commit != current_commit,
            "author": author,
            "message": message,
            "files_changed": changed_files,
            "affected_services": services,
            "affected_units": mapper.affected_units(changed_files),
            "smoke_tags": mapper.smoke_tags(changed_files),
        }

    def mark_deployed(self, commit: str, report_path: str | Path | None = None) -> None:
        path = Path(self.repo_path) / self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "branch": self.branch,
            "remote": self.remote,
            "deployed_commit": commit,
            "report_path": str(report_path) if report_path else "",
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def changed_files(self, previous_commit: str, current_commit: str, runner: CommandRunner | None = None) -> list[str]:
        if previous_commit == current_commit:
            return []
        output = self._git(["diff", "--name-only", f"{previous_commit}..{current_commit}"], runner or self.runner or CommandRunner())
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _previous_commit(self) -> str:
        path = Path(self.repo_path) / self.state_path
        if not path.exists():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ""
        return str(payload.get("deployed_commit") or "")

    def _git(
        self,
        args: list[str],
        runner: CommandRunner,
        *,
        allow_failure: bool = False,
    ) -> str:
        result = runner.run(["git", *args], cwd=self.repo_path, timeout=180)
        if not result.ok and not allow_failure:
            raise RuntimeError(result.stderr or result.stdout or f"git {' '.join(args)} failed")
        return result.stdout
