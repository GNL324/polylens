from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

CANONICAL_REPO = Path("/home/noel/polylens")
DEFAULT_REMOTE = "gitea"
DEFAULT_MAIN_REF = "main"
DEPLOYMENT_DRIFT_ALERT_CODE = "deployment_drift_detected"

MONITORED_SERVICES: tuple[str, ...] = (
    "polylens-telegram-console.service",
    "polylens-dashboard.service",
    "wallet-autonomy.service",
)

# Untracked paths that are expected on a live deployment checkout.
APPROVED_UNTRACKED_PATTERNS: tuple[str, ...] = (
    "data/*.db",
    "data/*.db-*",
    "data/traders.db.malformed.*",
    "data/wallets/*.json",
    "data/reports/*",
    "data/raw/*",
    "logs/*.log",
    "traders.db",
    ".pytest_cache/*",
    "**/__pycache__/*",
)


@dataclass
class DriftFinding:
    level: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def git_rev_parse(repo: Path, ref: str) -> str | None:
    result = _run_git(repo, "rev-parse", ref)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_symbolic_branch(repo: Path) -> str | None:
    result = _run_git(repo, "symbolic-ref", "--short", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_status_porcelain(repo: Path) -> list[str]:
    result = _run_git(repo, "status", "--porcelain", "-uall")
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def git_is_ignored(repo: Path, path: str) -> bool:
    result = _run_git(repo, "check-ignore", "-q", "--", path)
    return result.returncode == 0


def is_approved_untracked_path(path: str, *, repo: Path = CANONICAL_REPO) -> bool:
    return is_approved_untracked_path_for_repo(repo, path.strip().strip('"'))


def parse_working_directory(unit_text: str) -> str | None:
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("WorkingDirectory="):
            return stripped.split("=", 1)[1].strip()
    return None


def read_systemd_working_directory(service_name: str, *, repo: Path) -> str | None:
    result = subprocess.run(
        ["systemctl", "show", service_name, "--property=WorkingDirectory", "--value"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    unit_path = repo / "deploy" / "systemd" / service_name
    if unit_path.exists():
        return parse_working_directory(unit_path.read_text(encoding="utf-8"))
    return None


def unapproved_dirty_paths(repo: Path, status_lines: Sequence[str]) -> list[str]:
    unapproved: list[str] = []
    for line in status_lines:
        if len(line) < 4:
            continue
        index = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        if index == "??":
            if is_approved_untracked_path_for_repo(repo, path):
                continue
            unapproved.append(path)
            continue
        unapproved.append(path)
    return unapproved


def is_approved_untracked_path_for_repo(repo: Path, path: str) -> bool:
    if git_is_ignored(repo, path):
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in APPROVED_UNTRACKED_PATTERNS)


def fetch_remote_main(repo: Path, remote: str, main_ref: str) -> str | None:
    _run_git(repo, "fetch", remote, main_ref)
    return git_rev_parse(repo, f"{remote}/{main_ref}")


def check_deployment_drift(
    repo: Path = CANONICAL_REPO,
    *,
    remote: str = DEFAULT_REMOTE,
    main_ref: str = DEFAULT_MAIN_REF,
    canonical_repo: Path = CANONICAL_REPO,
    services: Sequence[str] = MONITORED_SERVICES,
    fetch: bool = True,
) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    drift_reasons: list[str] = []
    details: dict[str, Any] = {
        "repo": str(repo),
        "remote": remote,
        "main_ref": main_ref,
        "canonical_repo": str(canonical_repo),
    }

    if not repo.is_dir() or not (repo / ".git").exists():
        return [
            DriftFinding(
                "alert",
                DEPLOYMENT_DRIFT_ALERT_CODE,
                f"Deployment checkout is not a git repository: {repo}",
                {"reason": "missing_git_repo", "repo": str(repo)},
            )
        ]

    head = git_rev_parse(repo, "HEAD")
    branch = git_symbolic_branch(repo)
    details["head"] = head
    details["branch"] = branch

    remote_main = fetch_remote_main(repo, remote, main_ref) if fetch else git_rev_parse(repo, f"{remote}/{main_ref}")
    details["remote_main"] = remote_main
    details["remote_ref"] = f"{remote}/{main_ref}"

    if remote_main is None:
        findings.append(
            DriftFinding(
                "warning",
                "deployment_remote_main_unavailable",
                f"Could not resolve {remote}/{main_ref}; drift against main could not be verified.",
                {"remote_ref": f"{remote}/{main_ref}"},
            )
        )
    elif head != remote_main:
        drift_reasons.append("head_not_at_remote_main")
        details["commits_behind_ahead"] = {"head": head, "remote_main": remote_main}

    if branch is None:
        drift_reasons.append("detached_head")
    elif branch != main_ref:
        drift_reasons.append("non_main_branch")
        details["expected_branch"] = main_ref

    dirty = unapproved_dirty_paths(repo, git_status_porcelain(repo))
    if dirty:
        drift_reasons.append("dirty_worktree")
        details["dirty_paths"] = dirty

    service_dirs: dict[str, str | None] = {}
    wrong_service_dirs: list[str] = []
    for service in services:
        working_dir = read_systemd_working_directory(service, repo=repo)
        service_dirs[service] = working_dir
        if working_dir != str(canonical_repo):
            drift_reasons.append("wrong_working_directory")
            wrong_service_dirs.append(service)
    details["service_working_directories"] = service_dirs
    if wrong_service_dirs:
        details["wrong_service_units"] = wrong_service_dirs
        details["expected_working_directory"] = str(canonical_repo)

    if drift_reasons:
        message = _build_drift_message(
            branch=branch,
            head=head,
            remote_main=remote_main,
            drift_reasons=drift_reasons,
            dirty=dirty,
            wrong_service_dirs=wrong_service_dirs,
        )
        findings.insert(
            0,
            DriftFinding(
                "alert",
                DEPLOYMENT_DRIFT_ALERT_CODE,
                message,
                {**details, "reasons": drift_reasons},
            ),
        )
    else:
        findings.insert(
            0,
            DriftFinding(
                "info",
                "deployment_revision_ok",
                f"Deployed checkout matches {remote}/{main_ref} with a clean worktree.",
                details,
            ),
        )

    return findings


def _build_drift_message(
    *,
    branch: str | None,
    head: str | None,
    remote_main: str | None,
    drift_reasons: list[str],
    dirty: list[str],
    wrong_service_dirs: list[str],
) -> str:
    parts: list[str] = []
    if "non_main_branch" in drift_reasons:
        parts.append(f"branch={branch!r} (expected 'main')")
    if "detached_head" in drift_reasons:
        parts.append(f"detached HEAD at {head}")
    if "head_not_at_remote_main" in drift_reasons and remote_main and head:
        short_head = head[:12]
        short_main = remote_main[:12]
        parts.append(f"HEAD {short_head} != gitea/main {short_main}")
    if "dirty_worktree" in drift_reasons:
        preview = ", ".join(dirty[:3])
        suffix = "..." if len(dirty) > 3 else ""
        parts.append(f"dirty worktree ({preview}{suffix})")
    if "wrong_working_directory" in drift_reasons:
        parts.append(f"systemd WorkingDirectory mismatch for {', '.join(wrong_service_dirs)}")
    summary = "; ".join(parts) if parts else "deployment drift detected"
    return f"deployment_drift_detected: {summary}"


def deployment_drift_report(
    repo: Path = CANONICAL_REPO,
    *,
    remote: str = DEFAULT_REMOTE,
    main_ref: str = DEFAULT_MAIN_REF,
    canonical_repo: Path = CANONICAL_REPO,
    services: Sequence[str] = MONITORED_SERVICES,
    fetch: bool = True,
) -> dict[str, Any]:
    findings = check_deployment_drift(
        repo,
        remote=remote,
        main_ref=main_ref,
        canonical_repo=canonical_repo,
        services=services,
        fetch=fetch,
    )
    alert_count = sum(1 for finding in findings if finding.level == "alert")
    warning_count = sum(1 for finding in findings if finding.level == "warning")
    return {
        "status": "healthy" if alert_count == 0 else "alert",
        "alert_count": alert_count,
        "warning_count": warning_count,
        "findings": [finding.to_dict() for finding in findings],
    }
