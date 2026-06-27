#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

export PYTHONPATH=.

.venv/bin/python scripts/validate_runtime_db_isolation.py -- bash -c '
  set -euo pipefail
  echo "== targeted telegram/paper tests =="
  .venv/bin/python -m pytest tests/test_telegram_console.py tests/test_telegram_notifications.py tests/test_paper*.py -q

  echo "== full suite excluding known wallet autonomy hangs =="
  .venv/bin/python -m pytest -q -k '"'"'not test_run_due_cycles_records_service_state and not test_wallet_service_run_cli_force'"'"'
'
