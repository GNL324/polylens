# Security Audit

## Scope

This audit searched the repository for common secret indicators such as API keys, tokens, passwords, bearer tokens, webhook URLs, and credential variable names. It did not rewrite git history.

## Findings

No committed API key values or Telegram token values were identified during this OSS-readiness pass.

Expected non-secret references were found in documentation and source code, including environment variable names:

- `ODDS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `POLYLENS_WEBHOOK_URL`

An untracked deployment env file exists:

- `deploy/systemd/polylens-live-arb.env`

This file should not be committed unless it is sanitized or replaced by the existing `.env.example` style file.

## Recommendations

- Keep runtime env files untracked.
- Ensure `.gitignore` covers `data/*.db`, `data/raw/`, `data/reports/`, `logs/`, and deployment env files with secrets.
- Run a dedicated tool such as `gitleaks` or `trufflehog` before public release.
- Rotate any credentials that were ever pasted into issue trackers, chat logs, or committed history.
