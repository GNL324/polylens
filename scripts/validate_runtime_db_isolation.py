#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.testing.runtime_db_guard import (  # noqa: E402
    DEFAULT_PYTEST_ARGS,
    default_pytest_command,
    run_subprocess,
    validate_runtime_db_isolation,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify pytest workloads do not mutate production runtime SQLite DBs under data/.",
    )
    parser.add_argument(
        "--pytest-args",
        default=None,
        help="pytest arguments for the default representative workload (ignored when a command is supplied)",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python interpreter for the default pytest workload",
    )
    parser.add_argument(
        "--skip-workload",
        action="store_true",
        help="Compare snapshots without running pytest or a wrapped command",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Optional command to run between snapshots (prefix with --)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = [item for item in args.command if item != "--"]

    def workload() -> int:
        if args.skip_workload:
            return 0
        if command:
            return run_subprocess(command)
        interpreter = default_pytest_command(args.python)[0]
        pytest_tail = shlex.split(args.pytest_args) if args.pytest_args else list(DEFAULT_PYTEST_ARGS[2:])
        pytest_command = [interpreter, *DEFAULT_PYTEST_ARGS[:2], *pytest_tail]
        return run_subprocess(pytest_command)

    exit_code, report = validate_runtime_db_isolation(workload)
    print(report)
    if exit_code != 0:
        return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
