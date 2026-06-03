# Security Audit

## Scope

This audit searched the repository for common secret indicators such as API keys, tokens, passwords, bearer tokens, private keys, Telegram credentials, and Odds API environment variable names. It did not rewrite git history.

## Scan Command

Preferred command:

```bash
rg -n --hidden --glob '!.git' --glob '!data/raw/**' --glob '!data/opportunities.db'   'api[_-]?key|secret|token|password|private[_-]?key|TELEGRAM|ODDS_API_KEY|BEGIN .*PRIVATE KEY' .
```

If `rg` is unavailable, grep fallback is used with the same exclusions where practical.

## Findings

No committed secret values were identified in the release scan.

Expected non-secret references remain in documentation, examples, and source code as environment variable names:

- `ODDS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `POLYLENS_WEBHOOK_URL`

These are configuration names, not secret values.

## Runtime Artifacts

The following runtime artifacts must remain untracked or ignored for public release:

- `deploy/systemd/polylens-live-arb.env`
- raw API payloads under `data/raw/`
- generated reports under `data/reports/`
- logs under `logs/`
- SQLite DB files such as `data/polylens.db` and `data/opportunities.db`

The deployment env file is currently untracked and was not committed.

## Recommendations

- Keep runtime env files untracked.
- Ensure `.gitignore` covers SQLite DBs, raw data, reports, logs, and secret-bearing env files.
- Run a dedicated tool such as `gitleaks` or `trufflehog` before the first public GitHub release.
- Rotate any credentials that were ever pasted into chat, shell history, issue trackers, or committed history.
