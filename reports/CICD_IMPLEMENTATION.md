# Polylens Homelab CI/CD Automation Implementation

Date: 2026-06-25
Base: current `gitea/main`
Scope: infrastructure automation only

## Architecture

Implemented a new `src.cicd` package for the homelab development workflow:

- `detector.py`: detects changed commits/files on Gitea `main` and resolves affected runtime services.
- `service_map.py`: maps changed paths to runtime services and smoke-test tags.
- `engine.py`: plans or executes Predix deployments. Execution requires explicit `--execute`.
- `smoke.py`: runs structured smoke checks for wallet health, wallet alpha reports, Telegram daily report dry run, dashboard health, systemd state, and database connectivity.
- `reports.py`: creates structured JSON and Markdown deployment reports.
- `telegram.py`: formats success/failure deployment notifications and Mission Control/Grafana/report buttons.
- `brain.py`: idempotently archives deployment reports through a manifest and optional Brain ingestion command.
- `dashboard.py`: provides a read-only deployment status snapshot and NiceGUI `/deployments` page.
- `rollback.py`: creates and validates rollback plans. Rollbacks are not automatic unless explicitly configured.
- `runner.py`: shared command result/runner abstraction for testable system commands.

## Files Changed

- `src/cicd/__init__.py`
- `src/cicd/brain.py`
- `src/cicd/dashboard.py`
- `src/cicd/detector.py`
- `src/cicd/engine.py`
- `src/cicd/reports.py`
- `src/cicd/rollback.py`
- `src/cicd/runner.py`
- `src/cicd/service_map.py`
- `src/cicd/smoke.py`
- `src/cicd/telegram.py`
- `src/cli.py`
- `src/integrations/telegram_notifications.py`
- `src/web/app.py`
- `tests/test_cicd_platform.py`
- `reports/CICD_IMPLEMENTATION.md`

## Workflow

Feature branch development remains on SuperPC. Hermes can run local CI, push to Gitea, review, merge, and then use the CI/CD platform to detect the new `main` commit and deploy only affected services to Predix.

CLI commands added:

```bash
python -m src.cli cicd-detect --json
python -m src.cli cicd-smoke --json
python -m src.cli cicd-deploy --json
python -m src.cli cicd-deploy --execute --json
python -m src.cli cicd-status --json
```

`cicd-deploy` defaults to dry-run planning. It only runs `git pull`, service restarts, and smoke checks on Predix when Hermes passes `--execute`.

## Service Map

- Telegram: `src/integrations/telegram*`, `src/notifications/telegram.py`, Telegram systemd files, Telegram tests.
  - Units: `polylens-telegram-console.service`, `polylens-telegram-daily-report.service`
- Dashboard: `src/web/**`, `src/dashboard.py`, Grafana/dashboard deployment files, dashboard tests.
  - Units: `polylens-dashboard.service`, `polylens-trader-dashboard.service`
- Wallet autonomy/intelligence: wallet and trader-signal intelligence files, wallet tests, wallet autonomy systemd files.
  - Units: `wallet-autonomy.service`
- Analytics/services: adapters, alerts, analysis, storage, risk, services, trading, prop/paper/Kalshi/short-crypto systemd files.
  - Units: `polylens-live-arb.service`, `polylens-paper-trading.service`, `polylens-prop-arb-collector.service`, `polylens-prop-watch.service`, `polylens-short-crypto-paper.service`, `polylens-short-crypto-paper-settle.service`, `polylens-trader-signal-cycle.service`, `kalshi-market-recorder.service`

## Deployment Flow

1. Fetch Gitea `main`.
2. Compare the latest commit with `reports/deployments/state.json`.
3. Inspect changed files.
4. Resolve affected services and systemd units.
5. In dry-run mode, generate the exact Predix commands without mutating runtime.
6. In execute mode:
   - `git fetch --all --prune`
   - `git pull --ff-only`
   - restart only affected systemd units
   - run structured smoke checks
7. Generate JSON and Markdown deployment reports.
8. Archive report idempotently for Brain ingestion.
9. Send Telegram success/failure notification when Hermes invokes notification delivery.

## Smoke Tests

Structured smoke runner supports:

- `wallet-service-health`
- `wallet-alpha-report`
- `wallet-alpha-rankings`
- `telegram-daily-report --dry-run`
- dashboard health via Mission Control HTTP check
- systemd `is-active` checks for affected units
- SQLite database connectivity

Each smoke result includes command, status, return code, duration, stdout/stderr excerpts, and parsed JSON when available.

## Rollback Flow

Rollback planning is implemented but not automatically executed by default.

On deployment failure, the platform can generate a rollback plan containing:

- previous commit
- failed commit
- affected services
- validation errors, if any
- commands required to restore the previous commit and restart affected services

Automatic rollback requires explicit configuration through the deployment engine. The default behavior is report-and-notify only.

## Dashboard

The existing NiceGUI app now registers `/deployments`.

The page is read-only and shows:

- current commit
- current branch
- deployment history
- latest deployment
- health summary
- failures
- rollback history

The CLI equivalent is:

```bash
python -m src.cli cicd-status --json
```

## Brain Integration

Deployment reports are archived with `BrainDeploymentArchiver`.

Duplicate avoidance uses SHA-256 digests stored in:

```bash
reports/deployments/brain_archive_manifest.json
```

If `POLYLENS_BRAIN_INGEST_COMMAND` is configured, the archiver invokes it with the report path. Without that variable, the manifest still records the report idempotently for later ingestion by Hermes/Brain.

## Telegram Notifications

`TelegramNotificationService` now supports:

- `send_deployment_success(report, report_url=...)`
- `send_deployment_failure(report, report_url=...)`

Notifications include:

- branch
- commit
- author
- health
- services
- warnings/errors
- rollback recommendation

Buttons:

- Mission Control
- Grafana
- Deployment Report

## Tests

Added `tests/test_cicd_platform.py` covering:

- deployment detector changed-file detection
- service mapping
- smoke runner structured output
- deployment reports
- Telegram deployment notifications
- rollback planner
- Brain archival duplicate avoidance
- dashboard status snapshot

Validation run:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_cicd_platform.py -q
```

Result:

```text
8 passed in 0.07s
```

Full requested validation:

```bash
scripts/dev_ci.sh
```

Result:

```text
targeted telegram/paper tests: 97 passed in 0.38s
full suite excluding known wallet autonomy hangs: 1128 passed, 2 deselected in 10.59s
```

Known excluded tests remain:

- `test_run_due_cycles_records_service_state`
- `test_wallet_service_run_cli_force`

## Future Improvements

- Add a Gitea webhook receiver or systemd timer for Hermes to invoke `cicd-detect` and `cicd-deploy`.
- Publish deployment reports back to Gitea as PR or commit statuses.
- Configure `POLYLENS_BRAIN_INGEST_COMMAND` once the Brain ingestion command is finalized.
- Add authenticated internal links for deployment report URLs if reports are served through Mission Control.
- Add a human approval gate before `--execute` deployments from a webhook event.
- Add optional rollback execution after Hermes validates rollback policy and approval flow.
