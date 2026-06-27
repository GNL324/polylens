from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.deployment.drift_check import (
    DEPLOYMENT_DRIFT_ALERT_CODE,
    check_deployment_drift,
    is_approved_untracked_path_for_repo,
    parse_working_directory,
    unapproved_dirty_paths,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "drift@test.local")
    _git(repo, "config", "user.name", "drift-test")
    (repo / "README.md").write_text("polylens\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "-M", "main")


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_unit(repo: Path, service: str, working_dir: str) -> None:
    unit_dir = repo / "deploy" / "systemd"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / service).write_text(
        f"[Service]\nWorkingDirectory={working_dir}\n",
        encoding="utf-8",
    )


def _set_remote_main(repo: Path, sha: str) -> None:
    _git(repo, "update-ref", "refs/remotes/gitea/main", sha)


@pytest.fixture
def deployed_repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "polylens"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo, "main.txt")
    _write_unit(repo, "polylens-telegram-console.service", str(repo))
    _write_unit(repo, "polylens-dashboard.service", str(repo))
    _write_unit(repo, "wallet-autonomy.service", str(repo))
    _git(repo, "add", "deploy")
    _git(repo, "commit", "-m", "systemd units")
    main_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _set_remote_main(repo, main_sha)

    def _working_dir(service: str, *, repo: Path) -> str | None:
        unit_path = repo / "deploy" / "systemd" / service
        return parse_working_directory(unit_path.read_text(encoding="utf-8"))

    monkeypatch.setattr("src.deployment.drift_check.read_systemd_working_directory", _working_dir)
    return repo


def test_clean_deployed_main_passes(deployed_repo: Path):
    findings = check_deployment_drift(
        deployed_repo,
        canonical_repo=deployed_repo,
        fetch=False,
    )

    assert findings[0].code == "deployment_revision_ok"
    assert findings[0].level == "info"
    assert not any(finding.code == DEPLOYMENT_DRIFT_ALERT_CODE for finding in findings)


def test_stale_branch_alerts(deployed_repo: Path):
    _commit(deployed_repo, "newer-main.txt")
    main_sha = _git(deployed_repo, "rev-parse", "main").stdout.strip()
    _set_remote_main(deployed_repo, main_sha)
    _git(deployed_repo, "checkout", "-b", "feature/btc-5m-momentum-profile-clean", "HEAD~1")

    findings = check_deployment_drift(
        deployed_repo,
        canonical_repo=deployed_repo,
        fetch=False,
    )
    drift = next(f for f in findings if f.code == DEPLOYMENT_DRIFT_ALERT_CODE)

    assert drift.level == "alert"
    assert "non_main_branch" in drift.details["reasons"]
    assert "head_not_at_remote_main" in drift.details["reasons"]
    assert "feature/btc-5m-momentum-profile-clean" in drift.message


def test_detached_stale_head_alerts(deployed_repo: Path):
    stale_sha = _git(deployed_repo, "rev-parse", "HEAD").stdout.strip()
    _commit(deployed_repo, "newer-main.txt")
    main_sha = _git(deployed_repo, "rev-parse", "main").stdout.strip()
    _set_remote_main(deployed_repo, main_sha)
    _git(deployed_repo, "checkout", stale_sha)

    findings = check_deployment_drift(
        deployed_repo,
        canonical_repo=deployed_repo,
        fetch=False,
    )
    drift = next(f for f in findings if f.code == DEPLOYMENT_DRIFT_ALERT_CODE)

    assert "detached_head" in drift.details["reasons"]
    assert "head_not_at_remote_main" in drift.details["reasons"]


def test_dirty_tree_alerts(deployed_repo: Path):
    (deployed_repo / "src" / "cli.py").parent.mkdir(parents=True)
    (deployed_repo / "src" / "cli.py").write_text("dirty\n", encoding="utf-8")
    _git(deployed_repo, "add", "src/cli.py")
    _git(deployed_repo, "commit", "-m", "track cli")
    main_sha = _git(deployed_repo, "rev-parse", "HEAD").stdout.strip()
    _set_remote_main(deployed_repo, main_sha)
    (deployed_repo / "src" / "cli.py").write_text("modified\n", encoding="utf-8")

    findings = check_deployment_drift(
        deployed_repo,
        canonical_repo=deployed_repo,
        fetch=False,
    )
    drift = next(f for f in findings if f.code == DEPLOYMENT_DRIFT_ALERT_CODE)

    assert "dirty_worktree" in drift.details["reasons"]
    assert "src/cli.py" in drift.details["dirty_paths"]


def test_approved_runtime_artifacts_do_not_trigger_dirty_tree(deployed_repo: Path):
    (deployed_repo / "data").mkdir()
    (deployed_repo / "data" / "traders.db").write_bytes(b"sqlite")
    (deployed_repo / "logs").mkdir()
    (deployed_repo / "logs" / "polylens.log").write_text("log\n", encoding="utf-8")

    findings = check_deployment_drift(
        deployed_repo,
        canonical_repo=deployed_repo,
        fetch=False,
    )

    assert findings[0].code == "deployment_revision_ok"


def test_wrong_working_directory_alerts(deployed_repo: Path, tmp_path: Path):
    wrong = tmp_path / "wrong-checkout"
    wrong.mkdir()
    _write_unit(deployed_repo, "polylens-telegram-console.service", str(wrong))

    findings = check_deployment_drift(
        deployed_repo,
        canonical_repo=deployed_repo,
        fetch=False,
    )
    drift = next(f for f in findings if f.code == DEPLOYMENT_DRIFT_ALERT_CODE)

    assert "wrong_working_directory" in drift.details["reasons"]
    assert "polylens-telegram-console.service" in drift.details["wrong_service_units"]


def test_parse_working_directory():
    assert parse_working_directory("[Service]\nWorkingDirectory=/home/noel/polylens\n") == "/home/noel/polylens"


def test_unapproved_dirty_paths_ignores_runtime_db(deployed_repo: Path):
    assert is_approved_untracked_path_for_repo(deployed_repo, "data/traders.db")
    assert unapproved_dirty_paths(deployed_repo, ["?? data/traders.db"]) == []
    assert unapproved_dirty_paths(deployed_repo, [" M src/cli.py"]) == ["src/cli.py"]
