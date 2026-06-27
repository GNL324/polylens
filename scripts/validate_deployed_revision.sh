#!/usr/bin/env bash
# Read-only deployment drift guard for the Polylens production checkout.
set -euo pipefail

REPO_ROOT="${POLYLENS_REPO_ROOT:-/home/noel/polylens}"
cd "$REPO_ROOT"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

exec env PYTHONPATH="$REPO_ROOT" "$PYTHON" - "$@" <<'PY'
from __future__ import annotations

import json
import sys

from src.deployment.drift_check import CANONICAL_REPO, deployment_drift_report

json_mode = "--json" in sys.argv[1:]
report = deployment_drift_report(CANONICAL_REPO, fetch=True)
if json_mode:
    print(json.dumps(report, indent=2, sort_keys=True))
else:
    print(f"status={report['status']} alerts={report['alert_count']} warnings={report['warning_count']}")
    for finding in report["findings"]:
        print(f"{finding['level'].upper()} {finding['code']}: {finding['message']}")
raise SystemExit(0 if report["alert_count"] == 0 else 1)
PY
