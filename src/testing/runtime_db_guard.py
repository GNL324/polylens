from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
MISSING = "MISSING"

from src.testing.runtime_db_redirect import PRODUCTION_RUNTIME_DB_BY_NAME  # noqa: E402

RUNTIME_DB_FILES: dict[str, Path] = {
    name: path for name, path in PRODUCTION_RUNTIME_DB_BY_NAME.items()
}

DEFAULT_PYTEST_ARGS: tuple[str, ...] = (
    "-m",
    "pytest",
    "tests/test_runtime_db_isolation.py",
    "-q",
)


@dataclass(frozen=True)
class ValidationFailure:
    db_name: str
    kind: str
    expected: str
    actual: str

    def message(self) -> str:
        if self.kind == "created":
            return f"{self.db_name} created unexpectedly"
        if self.kind == "deleted":
            return f"{self.db_name} deleted unexpectedly"
        return (
            f"{self.db_name} hash changed\n\n"
            f"Expected:\n{self.expected}\n\n"
            f"Actual:\n{self.actual}"
        )


def digest_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_runtime_dbs() -> dict[str, str]:
    """Return db filename -> SHA256 hex digest, or MISSING when absent."""
    return {
        name: digest if (digest := digest_file(path)) is not None else MISSING
        for name, path in RUNTIME_DB_FILES.items()
    }


def compare_snapshots(
    before: dict[str, str],
    after: dict[str, str],
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    for name in RUNTIME_DB_FILES:
        expected = before.get(name, MISSING)
        actual = after.get(name, MISSING)
        if expected == actual:
            continue
        if expected == MISSING and actual != MISSING:
            failures.append(ValidationFailure(name, "created", MISSING, actual))
        elif expected != MISSING and actual == MISSING:
            failures.append(ValidationFailure(name, "deleted", expected, MISSING))
        else:
            failures.append(ValidationFailure(name, "changed", expected, actual))
    return failures


def format_validation_report(
    before: dict[str, str],
    after: dict[str, str],
    failures: Sequence[ValidationFailure],
) -> str:
    failed_names = {item.db_name for item in failures}
    lines = ["Runtime DB Validation", ""]
    for name in RUNTIME_DB_FILES:
        if name in failed_names:
            continue
        lines.append(f"✓ {name} unchanged")
    if failures:
        lines.append("")
        lines.append("FAIL")
        lines.append("")
        for item in failures:
            lines.append(item.message())
            lines.append("")
        return "\n".join(lines).rstrip()
    lines.append("")
    lines.append("PASS")
    return "\n".join(lines)


def default_pytest_command(python: str | None = None) -> list[str]:
    interpreter = python or sys.executable
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if python is None and venv_python.exists():
        interpreter = str(venv_python)
    return [interpreter, *DEFAULT_PYTEST_ARGS]


def run_subprocess(command: Sequence[str], *, cwd: Path | None = None) -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    completed = subprocess.run(
        list(command),
        cwd=str(cwd or REPO_ROOT),
        env=env,
        check=False,
    )
    return int(completed.returncode)


def validate_runtime_db_isolation(
    workload: Callable[[], int] | None = None,
    *,
    before: dict[str, str] | None = None,
    after: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run workload between snapshots; return (exit_code, report_text)."""
    start = before if before is not None else snapshot_runtime_dbs()
    exit_code = 0
    if workload is not None:
        exit_code = workload()
    end = after if after is not None else snapshot_runtime_dbs()
    failures = compare_snapshots(start, end)
    report = format_validation_report(start, end, failures)
    if failures:
        return 1, report
    return exit_code, report
