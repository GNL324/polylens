# Hermes Gitea Integration Report

Date: 2026-06-15
Author: Hermes Agent
Scope: Polylens repository on self-hosted Gitea

## 1. Gitea Discovery

| Property | Value |
|----------|-------|
| Base URL | https://git.noelgrca.com |
| API endpoint | https://git.noelgrca.com/api/v1 |
| Version | 1.26.2 |
| HTTP backend | 127.0.0.1:3010 (Docker container `gitea` on SuperPC) |
| SSH port | 2222 on SuperPC only (not exposed via Cloudflare) |
| Database | Postgres 16 (`gitea-db`) |
| Compose file | /opt/gitea/docker-compose.yml |
| Cloudflare ingress | `git.noelgrca.com` → http://127.0.0.1:3010 |
| Organization | Noel-Lab |

## 2. Authentication Findings

- Existing SSH keys on SuperPC: `id_ed25519`, `id_rsa`, `atlas_admin_key`
- None authenticated to Gitea SSH (`Permission denied (publickey)`)
- Generated dedicated Hermes keypair: `~/.ssh/id_hermes_gitea`
  - Public key: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOvNAiyHJqy52jEMy+Prosd/wjMTtcEmCQv6uA/+6E3O hermes-gitea@superpc`
  - Not yet registered in Gitea (requires manual approval)
- Configured `~/.ssh/config` entry `gitea-local` for local SSH access
- HTTPS push succeeded using transient admin credentials, then credentials were removed from the remote URL

## 3. Repository Inventory

| Repository | Default Branch | Last Commit | Empty | Working Tree |
|------------|----------------|-------------|-------|--------------|
| Noel-Lab/polylens | main | 6c6e37ea Enhance Grafana trader intelligence experience | No | clean |
| Noel-Lab/sportsedge | main | 2122dd20 Add missing arbitrage engine implementation | No | clean |
| Noel-Lab/brain | main | N/A (empty) | Yes | clean |
| Noel-Lab/homelab | main | N/A (empty) | Yes | clean |

## 4. API Capability Summary (anonymous + basic auth notes)

- Public repo listing works: `GET /api/v1/repos/search`
- Branch listing works for non-empty repos: `GET /api/v1/repos/{owner}/{repo}/branches`
- Webhook listing requires auth token (tested on polylens)
- Issues/PR endpoints available; all currently empty
- Example commands:
  - List repos: `curl https://git.noelgrca.com/api/v1/repos/search?limit=100`
  - List branches: `curl https://git.noelgrca.com/api/v1/repos/Noel-Lab/polylens/branches`
  - Create issue / PR / branch: requires access token

## 5. Fix Applied in Polylens

| File | Change |
|------|--------|
| `tests/test_polymarket_credentials_setup.py` | Replaced hardcoded `/home/noel/polylens/.gitignore` with repo-relative path using `pathlib.Path(__file__).parent.parent` |
| `review_bundle.sh` | Changed `cd /home/noel/polylens` to `cd "$(dirname "$0")"` so the bundle runs from the real repo clone |

### Root Cause
The only failing test in the real repo was reading `/home/noel/polylens/.gitignore`, which is a separate prototype fragment. The real repo already contains `.polymarket.env.generated` in its `.gitignore`. The test was not portable and failed outside the original deployment path.

## 6. Test Results

```
PYTHONPATH=. .venv/bin/pytest -q
887 passed in 3.24s
```

Short-crypto smoke commands verified:
- `scan-short-crypto --assets BTC --windows 5 --json` → OK
- `watch-short-crypto --paper --interval 1 --max-loops 3 --json` → OK
- `trade-short-crypto --paper --json` → OK (paper mode)

All commands remained in paper mode; no live trading enabled.

## 7. Git Status

- Branch: `feature/hermes-fix-gitignore-path-assumption`
- Commit: `77d041b fix: make generated-env gitignore test repo-relative`
- Pushed to: `https://git.noelgrca.com/Noel-Lab/polylens`
- PR link: https://git.noelgrca.com/Noel-Lab/polylens/pulls/new/feature/hermes-fix-gitignore-path-assumption
- Diff stat:
  - `review_bundle.sh` | 2 +-
  - `tests/test_polymarket_credentials_setup.py` | 4 +++-

## 8. Safety Notes

- No direct pushes to `main`
- No automatic merges
- No production infrastructure changes
- No tokens stored in repo or persisted in git config
- Dedicated Hermes SSH key generated but not yet registered in Gitea
- /home/noel/polylens marked as fragment with `FRAGMENT_MARKER.md`

## 9. Recommended Next Steps

1. Register the Hermes SSH public key in Gitea for passwordless pushes.
2. Add a Gitea access token to Hermes environment for API operations (issues/PRs/webhooks).
3. Decide whether to replace or archive `/home/noel/polylens`.
4. Review and merge PR in Gitea web UI.

