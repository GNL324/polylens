# Security

## Secret Handling

Never commit:

- `ODDS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- webhook URLs
- SSH credentials
- private wallet notes or private user data

Use environment variables or local env files excluded from git.

## Data Sensitivity

Raw API payloads can include wallet activity, bookmaker line metadata, and operational details. Treat `data/raw/`, `data/reports/`, SQLite databases, and `logs/` as local runtime artifacts unless explicitly sanitized.

## Responsible Usage

Polylens is analytics software. It does not place trades or bets. Users are responsible for complying with laws, venue terms, sportsbook rules, and tax/reporting obligations.

## Dependency Hygiene

Run tests before release and keep dependencies current. When adding dependencies, prefer well-maintained packages with clear licenses.
