from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.cicd.runner import CommandRunner


@dataclass(frozen=True)
class SmokeCheck:
    name: str
    command: tuple[str, ...]
    tags: tuple[str, ...]
    timeout: int = 180


DEFAULT_SMOKE_CHECKS: tuple[SmokeCheck, ...] = (
    SmokeCheck("wallet-service-health", ("python", "-m", "src.cli", "wallet-service-health", "--json"), ("wallet-service-health", "wallet")),
    SmokeCheck("wallet-alpha-report", ("python", "-m", "src.cli", "wallet-alpha-report", "--json"), ("wallet-alpha-report", "wallet")),
    SmokeCheck("wallet-alpha-rankings", ("python", "-m", "src.cli", "wallet-alpha-rankings", "--json"), ("wallet-alpha-rankings", "wallet")),
    SmokeCheck("telegram-daily-report", ("python", "-m", "src.cli", "telegram-daily-report", "--dry-run", "--json"), ("telegram-daily-report", "telegram")),
    SmokeCheck("dashboard-health", ("curl", "-fsS", "http://127.0.0.1:8787/mission-control"), ("dashboard-health", "dashboard"), 30),
    SmokeCheck("database-connectivity", ("sqlite3", "data/traders.db", "SELECT 1;"), ("database-connectivity", "database"), 30),
)


class SmokeRunner:
    def __init__(
        self,
        *,
        repo_path: str | Path = ".",
        runner: CommandRunner | None = None,
        checks: tuple[SmokeCheck, ...] = DEFAULT_SMOKE_CHECKS,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.runner = runner or CommandRunner()
        self.checks = checks

    def run(self, *, tags: list[str] | tuple[str, ...] | None = None, systemd_units: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        selected_tags = set(tags or [])
        checks = [check for check in self.checks if not selected_tags or selected_tags.intersection(check.tags)]
        results = [self._run_check(check) for check in checks]
        for unit in systemd_units or []:
            results.append(self._run_systemd_check(str(unit)))
        ok = all(item["ok"] for item in results)
        return {
            "ok": ok,
            "status": "passed" if ok else "failed",
            "checks": results,
            "summary": {
                "total": len(results),
                "passed": sum(1 for item in results if item["ok"]),
                "failed": sum(1 for item in results if not item["ok"]),
            },
        }

    def _run_check(self, check: SmokeCheck) -> dict[str, Any]:
        started = time.monotonic()
        result = self.runner.run(list(check.command), cwd=self.repo_path, timeout=check.timeout)
        duration = round(time.monotonic() - started, 3)
        payload: dict[str, Any] = {
            "name": check.name,
            "tags": list(check.tags),
            "command": list(check.command),
            "ok": result.ok,
            "returncode": result.returncode,
            "duration_seconds": duration,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }
        parsed = _parse_json(result.stdout)
        if parsed is not None:
            payload["json"] = parsed
        return payload

    def _run_systemd_check(self, unit: str) -> dict[str, Any]:
        check = SmokeCheck(f"systemd:{unit}", ("systemctl", "is-active", unit), ("systemd",), 30)
        return self._run_check(check)


def _parse_json(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
