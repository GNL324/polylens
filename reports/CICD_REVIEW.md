# CICD PLATFORM REVIEW

**Branch:** Working tree on `/srv/devcloud/repos/polylens` (on `main`)
**Merge target:** `gitea/main`
**Files changed:** 3 modified + 15 new (12 src + 1 test + 2 reports + CI scripts)
**Date:** 2026-06-25

---

## Task Summary

Review the CI/CD automation platform for merge into `gitea/main`. Focus: CI/CD detector correctness, service mapping, dry-run safety, deployment execution gating, rollback planning safety, smoke test behavior, Telegram notification safety, Brain archival behavior, read-only deployment dashboard, no automatic deployment, no live trading changes, no signing, no private keys, no order placement, no execution approvals.

---

## Files Changed

| File | Status | Lines |
|---|---|---|
| `src/cicd/__init__.py` | New | 14 |
| `src/cicd/detector.py` | New | 87 |
| `src/cicd/service_map.py` | New | 143 |
| `src/cicd/engine.py` | New | 137 |
| `src/cicd/runner.py` | New | 54 |
| `src/cicd/smoke.py` | New | 91 |
| `src/cicd/rollback.py` | New | 64 |
| `src/cicd/reports.py` | New | 130 |
| `src/cicd/telegram.py` | New | 47 |
| `src/cicd/brain.py` | New | 70 |
| `src/cicd/dashboard.py` | New | 78 |
| `src/cli.py` | Modified | +86 |
| `src/integrations/telegram_notifications.py` | Modified | +18 |
| `src/web/app.py` | Modified | +2/-1 |
| `tests/test_cicd_platform.py` | New | 165 |
| `scripts/dev_ci.sh` | New | (documentation) |
| `scripts/ci_branch_review.sh` | New | (documentation) |
| `reports/CICD_IMPLEMENTATION.md` | New | implementation report |
| `reports/SUPERPC_CODEX_CI_SETUP.md` | New | setup guide |
| `reports/ci/main.md` | New | CI status |

---

## Implementation Summary

### CI/CD Detector (`src/cicd/detector.py`)

`GitDeploymentDetector` fetches from remote, compares current commit against last-deployed commit (stored in `reports/deployments/state.json`), computes changed files via `git diff --name-only`, and maps them to affected services via `ServiceMapper`.

**Correctness:** Falls back to `{current_commit}^` if no previous state exists. State file is JSON with `deployed_commit`. `mark_deployed()` updates state after successful deployment.

### Service Mapping (`src/cicd/service_map.py`)

Maps changed file paths to runtime services via `fnmatch` patterns. Four service groups:

| Service | Units | Path patterns |
|---|---|---|
| `telegram` | `polylens-telegram-console.service`, `polylens-telegram-daily-report.service` | `src/integrations/telegram*`, `deploy/systemd/polylens-telegram-*` |
| `dashboard` | `polylens-dashboard.service`, `polylens-trader-dashboard.service` | `src/web/**`, `deploy/systemd/polylens-dashboard*` |
| `wallet` | `wallet-autonomy.service` | `src/intelligence/wallet*`, `src/analysis/wallet*`, `src/analysis/trader_signal*` |
| `analytics` | `polylens-paper-trading.service`, `polylens-short-crypto-paper.*`, `polylens-trader-signal-cycle.service`, etc. | `src/analysis/**`, `src/trading/**`, `src/risk/**` |

**Correctness:** Path normalization handles Windows backslashes. Deduplication in `affected_units()` and `smoke_tags()`.

### Deployment Engine (`src/cicd/engine.py`)

`DeploymentEngineConfig` defaults:
- `execute: bool = False` — **dry-run by default** ✅
- `rollback_enabled: bool = False` — **rollback disabled by default** ✅

**Dry-run mode:** Generates deployment plan with commands list, adds warning `"dry-run only; pass --execute to mutate Predix"`, no SSH commands executed.

**Execute mode (`--execute`):** Runs SSH commands to Predix: `git fetch`, `git pull --ff-only`, `systemctl restart <unit>` for each affected unit. Then runs remote smoke tests.

### Rollback Planning (`src/cicd/rollback.py`)

`RollbackPlan` defaults to `enabled=False`. Commands are generated (`git reset --hard <previous>`, `systemctl restart <units>`) but **not executed** unless `enabled=True`. The `to_dict()` output explicitly states:
- `"will_execute_automatically": self.enabled` → `False` by default
- `"requires_explicit_configuration": not self.enabled` → `True` by default

**Rollback must not auto-execute unless explicitly configured.** ✅ Confirmed.

### Smoke Tests (`src/cicd/smoke.py`)

6 default checks:

| Check | Command | Tags |
|---|---|---|
| `wallet-service-health` | `python -m src.cli wallet-service-health --json` | `wallet-service-health`, `wallet` |
| `wallet-alpha-report` | `python -m src.cli wallet-alpha-report --json` | `wallet-alpha-report`, `wallet` |
| `wallet-alpha-rankings` | `python -m src.cli wallet-alpha-rankings --json` | `wallet-alpha-rankings`, `wallet` |
| `telegram-daily-report` | `python -m src.cli telegram-daily-report --dry-run --json` | `telegram-daily-report`, `telegram` |
| `dashboard-health` | `curl -fsS http://127.0.0.1:8787/mission-control` | `dashboard-health`, `dashboard` |
| `database-connectivity` | `sqlite3 data/traders.db SELECT 1;` | `database-connectivity`, `database` |

All checks are **read-only**. No mutations. Telegram daily report uses `--dry-run`.

### Telegram Notifications (`src/cicd/telegram.py`)

`format_deployment_success()` and `format_deployment_failure()` produce text summaries. `deployment_buttons()` generates inline URL buttons (Mission Control, Grafana, Deployment Report). Uses `safe_telegram_text()` via the notification service's `send_notification()`.

### Brain Archival (`src/cicd/brain.py`)

`BrainDeploymentArchiver` archives deployment reports to Brain VM. SHA-256 digest-based deduplication — duplicate reports are skipped. Optional `ingest_command` from env var `POLYLENS_BRAIN_INGEST_COMMAND`. Manifest stored as JSON.

**Idempotent:** `test_brain_archival_is_idempotent` verifies second archive returns `duplicate: True`.

### Deployment Dashboard (`src/cicd/dashboard.py`)

`create_deployment_status_page()` adds `/deployments` route to NiceGUI. Shows:
- Current branch/commit
- Health status
- Failure count
- Latest deployment (read-only JSON editor)
- Deployment history (read-only JSON editor)
- Rollback history (read-only JSON editor)

**Read-only:** All JSON editors use `.props("readonly")`. No write actions. ✅

### CLI Commands

| Command | Purpose | Dry-run default |
|---|---|---|
| `cicd-detect` | Detect new commits and affected services | N/A (read-only) |
| `cicd-smoke` | Run smoke tests locally | N/A (read-only) |
| `cicd-deploy` | Plan or execute deployment | ✅ `--execute` required for mutation |
| `cicd-status` | Read-only deployment status snapshot | N/A (read-only) |

---

## Safety Review

### Dry-Run Safety

| Check | Status | Evidence |
|---|---|---|
| `cicd-deploy` defaults to dry-run | ✅ | `DeploymentEngineConfig.execute = False` |
| Predix mutation requires `--execute` | ✅ | `if self.config.execute:` gate on line 45 |
| Dry-run adds explicit warning | ✅ | `"dry-run only; pass --execute to mutate Predix"` |
| Dry-run generates plan without SSH | ✅ | No `self._remote()` calls in dry-run path |

### Rollback Safety

| Check | Status | Evidence |
|---|---|---|
| Rollback disabled by default | ✅ | `RollbackPlan.enabled = False` |
| Rollback commands not auto-executed | ✅ | `will_execute_automatically: False` |
| Requires explicit `--allow-rollback` | ✅ | CLI `--allow-rollback` sets `rollback_enabled` |
| Rollback plan is advisory only | ✅ | Commands generated but not run in engine |

### No Trading/Signing/Keys/Orders/Approvals

| Check | Status | Evidence |
|---|---|---|
| No live trading changes | ✅ | `grep` clean across all `src/cicd/` files |
| No private-key handling | ✅ | `grep` clean |
| No wallet signing | ✅ | `grep` clean |
| No Polymarket order placement | ✅ | `grep` clean |
| No execution approvals | ✅ | `grep` clean |
| No DB mutations from CI/CD code | ✅ | `grep` for `INSERT|UPDATE|DELETE|CREATE|DROP|ALTER` — clean |
| No trading logic modified | ✅ | Only `cli.py` (new subcommands), `telegram_notifications.py` (new methods), `app.py` (new route) |

### Dashboard Read-Only

| Check | Status | Evidence |
|---|---|---|
| `/deployments` page is read-only | ✅ | JSON editors use `.props("readonly")` |
| No write actions on dashboard | ✅ | Only reads from `DeploymentReportStore.history()` |
| No service control from dashboard | ✅ | No systemctl calls in dashboard |

### Telegram Notification Safety

| Check | Status | Evidence |
|---|---|---|
| Token redaction | ✅ | Uses `send_notification()` which calls `safe_telegram_text()` |
| Buttons are URL links only | ✅ | No callback_data that could trigger actions |
| Audit logged | ✅ | `send_notification()` calls `audit_notification_delivery()` |

---

## Tests Run

### CI/CD platform tests:
```
PYTHONPATH=. .venv/bin/python -m pytest tests/test_cicd_platform.py -q
........                                                                 [100%]
8 passed in 0.05s
```

### Full suite (known exclusions):
```
PYTHONPATH=. .venv/bin/python -m pytest -q -k 'not test_run_due_cycles_records_service_state and not test_wallet_service_run_cli_force'
........................................................................ [  6%]
... (15 batches) ...
................................................                         [100%]
1128 passed, 2 deselected in 10.68s
```

### CI script:
```
scripts/dev_ci.sh
=== Compile check ===
=== Targeted tests (known exclusions applied) ===
97 passed in 0.36s
== full suite excluding known wallet autonomy hangs ==
1128 passed, 2 deselected in 10.46s
=== CI PASS ===
```

### Test Coverage

| Test | What it verifies |
|---|---|
| `test_deployment_detector_reads_changed_files` | Detector correctly identifies changed files between commits |
| `test_service_mapping_targets_only_affected_services` | Service mapper targets correct services, doesn't over-match |
| `test_smoke_runner_returns_structured_results` | Smoke runner returns structured JSON with pass/fail summary |
| `test_deployment_report_store_writes_json_and_markdown` | Report store saves both JSON and Markdown |
| `test_telegram_deployment_notifications_use_buttons` | Telegram notifications include correct buttons and format |
| `test_rollback_planner_requires_explicit_enablement` | Rollback defaults to disabled, deduplicates services |
| `test_brain_archival_is_idempotent` | Brain archiver skips duplicates via SHA-256 |
| `test_dashboard_status_snapshot_reads_history` | Dashboard reads deployment history correctly |

---

## Deselected Tests

Same pre-existing slow tests as previous reviews:
- `test_run_due_cycles_records_service_state` — passes in ~15s individually, not modified by this branch
- `test_wallet_service_run_cli_force` — same, not modified

Both unrelated to CI/CD platform code.

---

## Git Diff Stat

```
 src/cli.py                                 | 86 +++++++++++
 src/integrations/telegram_notifications.py | 18 ++++
 src/web/app.py                             |  3 +-
 src/cicd/ (12 new files)                   | 915 ++++++++++++++++
 tests/test_cicd_platform.py                | 165 +++++++++
 scripts/dev_ci.sh                           | new
 scripts/ci_branch_review.sh                 | new
 reports/CICD_IMPLEMENTATION.md              | new
 reports/SUPERPC_CODEX_CI_SETUP.md           | new
 reports/ci/main.md                          | new
```

---

## Concerns

1. **No automatic deployment during implementation** — confirmed. `cicd-deploy` without `--execute` is dry-run only. No cron job or webhook auto-triggers deployment.
2. **SSH key for Predix** — the engine uses `ssh noel@192.168.68.62` which relies on existing SSH key infrastructure. This is correct for the homelab but should be documented.
3. **Smoke test `wallet-alpha-report`** — this was recently optimized from 59s to <3s, so the 180s timeout is generous.
4. **Dashboard route** — `/deployments` is added to the existing web dashboard app. It's read-only and doesn't interfere with existing routes.

---

## Final Verdict

# ✅ MERGE

All safety checks pass:
- `cicd-deploy` defaults to dry-run (`execute=False`)
- Predix mutation requires explicit `--execute`
- Rollback disabled by default (`enabled=False`), requires `--allow-rollback`
- Rollback commands generated but not auto-executed
- Dashboard is read-only (`.props("readonly")` on all JSON editors)
- No live trading, signing, private keys, order placement, or execution approvals
- No DB mutations from CI/CD code
- No trading logic modified
- Telegram notifications use token redaction and audit logging
- Brain archival is idempotent (SHA-256 dedup)
- 8 CI/CD tests + 1128 full suite tests pass
- `scripts/dev_ci.sh` passes