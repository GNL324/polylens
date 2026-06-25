# SuperPC Codex CI Setup

Date: 2026-06-25T17:22:22-04:00
Host: superpc

## Role

SuperPC is configured as the Polylens build/development and local CI workstation.
Predix remains runtime. Brain remains memory/RAG.

## Installed Tools

- git: 2.47.3
- python3: 3.13.5
- venv Python: 3.13.5
- venv pip: 26.1.2
- pipx: 1.7.1
- node: v24.15.0
- npm: 11.12.1
- build-essential/gcc: 14.2.0
- sqlite3: 3.46.1
- ripgrep: 14.1.1
- jq: 1.7

Note: Node is installed from NodeSource. Its `nodejs` package includes npm and conflicts with Debian's separate `npm` package, so the standalone Debian `npm` package is not installed even though the `npm` command is available and validated.

## Codex CLI

- Installed package: `@openai/codex`
- Validation: `codex --version`
- Version: `codex-cli 0.142.2`

No browser login prompt appeared during install/version validation. ChatGPT sign-in should be completed from an interactive SuperPC shell when Codex first requests authentication, preferably with ChatGPT sign-in rather than stored API keys.

## Repository

- Repo path: `/srv/devcloud/repos/polylens`
- Branch: `main`
- HEAD: `e439a92 Add Telegram paper trading intelligence`
- Remote: `origin ssh://git@192.168.68.63:2222/Noel-Lab/polylens.git`
- SSH command: `ssh -i /root/.ssh/id_hermes_gitea -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new`

The `127.0.0.1:2222` Gitea route is configured in root's SSH config, but Predix's working Polylens remote uses `192.168.68.63:2222`; SuperPC was configured with that same working Gitea URL.

## Python Environment

- Venv path: `/srv/devcloud/repos/polylens/.venv`
- Installed project dependencies from `requirements.txt`
- Installed test runner: `pytest 9.1.1`

Attempted command: `.venv/bin/pip install -e . pytest`

Result: editable install could not be completed because the current Gitea `main` checkout has neither `pyproject.toml` nor `setup.py`. The CI script exports `PYTHONPATH=.` and tests pass with dependencies installed from `requirements.txt`.

## Local CI Scripts

Created and made executable:

- `/srv/devcloud/repos/polylens/scripts/dev_ci.sh`
- `/srv/devcloud/repos/polylens/scripts/ci_branch_review.sh`

`scripts/dev_ci.sh` runs:

- targeted Telegram/paper tests
- full suite excluding the known wallet autonomy hang tests

`scripts/ci_branch_review.sh <branch>`:

- fetches the requested branch from Gitea
- creates a clean detached worktree under `/srv/devcloud/worktrees/<branch>`
- links the main repo venv into the worktree
- runs the local dev CI script
- writes a markdown report under `/srv/devcloud/repos/polylens/reports/ci/<branch>.md`
- does not merge
- does not push to main

## Validation Results

Command:

```bash
cd /srv/devcloud/repos/polylens
scripts/dev_ci.sh
```

Result:

- targeted Telegram/paper tests: 97 passed in 0.75s
- full suite excluding known wallet autonomy hangs: 1120 passed, 2 deselected in 11.51s
- exit status: 0

Branch-review smoke test:

```bash
cd /srv/devcloud/repos/polylens
scripts/ci_branch_review.sh main
```

Result:

- worktree: `/srv/devcloud/worktrees/main`
- report: `/srv/devcloud/repos/polylens/reports/ci/main.md`
- targeted Telegram/paper tests: 97 passed in 0.76s
- full suite excluding known wallet autonomy hangs: 1120 passed, 2 deselected in 11.55s
- exit status: 0

## Known Skipped/Hanging Tests

The local CI excludes these known wallet autonomy hang tests:

- `test_run_due_cycles_records_service_state`
- `test_wallet_service_run_cli_force`

They are deselected by:

```bash
-k 'not test_run_due_cycles_records_service_state and not test_wallet_service_run_cli_force'
```

## Current Git Status

Expected local-only setup artifacts are untracked until committed:

- `scripts/dev_ci.sh`
- `scripts/ci_branch_review.sh`
- `reports/ci/main.md`
- `reports/SUPERPC_CODEX_CI_SETUP.md`

## Next Recommended Automation

Add a Gitea webhook or scheduled systemd timer on SuperPC that invokes:

```bash
cd /srv/devcloud/repos/polylens
scripts/ci_branch_review.sh <branch>
```

For a first pass, trigger it on pull-request or branch-push events and publish the generated markdown report back to Gitea as a PR comment/status without merging or pushing to `main`.
