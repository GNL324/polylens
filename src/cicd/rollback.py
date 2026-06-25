from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RollbackPlan:
    previous_commit: str
    failed_commit: str
    services: tuple[str, ...]
    enabled: bool = False

    def commands(self) -> list[str]:
        commands = [
            "git fetch --all --prune",
            f"git reset --hard {self.previous_commit}",
        ]
        commands.extend(f"systemctl restart {unit}" for unit in self.services)
        return commands

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "previous_commit": self.previous_commit,
            "failed_commit": self.failed_commit,
            "services": list(self.services),
            "commands": self.commands(),
            "will_execute_automatically": self.enabled,
            "requires_explicit_configuration": not self.enabled,
        }


class RollbackPlanner:
    def plan(
        self,
        *,
        previous_commit: str,
        failed_commit: str,
        services: list[str] | tuple[str, ...],
        enabled: bool = False,
    ) -> RollbackPlan:
        unique_services: list[str] = []
        for unit in services:
            if unit not in unique_services:
                unique_services.append(str(unit))
        return RollbackPlan(
            previous_commit=previous_commit,
            failed_commit=failed_commit,
            services=tuple(unique_services),
            enabled=enabled,
        )

    def validate(self, plan: RollbackPlan) -> dict[str, Any]:
        errors: list[str] = []
        if not plan.previous_commit:
            errors.append("previous commit is required")
        if not plan.failed_commit:
            errors.append("failed commit is required")
        if plan.previous_commit == plan.failed_commit:
            errors.append("previous and failed commits must differ")
        if not plan.services:
            errors.append("at least one affected service is required")
        return {"ok": not errors, "errors": errors, "plan": plan.to_dict()}
