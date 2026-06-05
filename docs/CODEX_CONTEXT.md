# Project Overview

Polylens is a research and paper-trading system for finding cross-market and sportsbook arbitrage opportunities. It ingests Polymarket, Kalshi, and sportsbook/Odds API data, normalizes markets and player props, scores opportunities, persists results, and exposes a local Command Center dashboard.

Major architecture:
- CLI entrypoint: `src/cli.py`
- Analysis/scanners: `src/analysis/`
- Storage: `src/storage/opportunity_store.py` for live/cross-market data and `src/storage/opportunities.py` for prop-arb, paper trades, lifecycle, and alert events
- Web dashboard: FastAPI + NiceGUI in `src/web/`
- Risk controls: `src/risk/`
- Notifications: `src/notifications/`
- Systemd deployment files: `deploy/systemd/`

# Current Features

- Scanners: wallet analysis, Polymarket/Kalshi matching, live arbitrage, true/multibook arbitrage, sportsbook futures, player props, and prop arbitrage.
- Dashboard: local NiceGUI Command Center with live opportunities, P&L, risk, bot controls, scanner status, alerts, operations, and opportunity explorer pages.
- Persistence: SQLite databases under `data/`, primarily `data/polylens.db` and `data/opportunities.db`.
- Paper trading: prop opportunities can be marked as paper trades, settled as over/under/push/void, and summarized in P&L analytics.
- Lifecycle: prop opportunities track `discovered`, `viewed`, `paper_traded`, `settled`, and `archived`.
- Operations: scanner health, scan metrics, service status, health badges, bookmaker analytics, and opportunity funnels.
- Alerts: `alert_events` records sent/skipped/duplicate/failed alert outcomes with secret redaction.
- Safety: live trading remains disabled; dashboard controls are strict allowlists and use `shell=False`.

# Database Schema

Key databases:
- `data/polylens.db`: live/cross-market scan runs, opportunities, alerts, rejected candidates, risk data.
- `data/opportunities.db`: prop-arb scan runs, prop opportunities, paper trades, alerts, and alert events.

Key tables:
- `prop_arbitrage_scan_runs`: scanner health and run metadata. Includes `started_at`, `finished_at`, `props_fetched`, `matched_pairs`, `rejected_pairs`, `opportunities_found`, `scan_duration`, `saved_opportunity_ids`, `duplicate_count`, `error_message`, and `raw_json`.
- `prop_arbitrage_opportunities`: persisted prop-arb opportunities. Linked to scan runs by `scan_run_id`; linked to paper trades by `paper_trade_id`.
- `paper_trades`: paper execution and settlement records linked by `opportunity_id`.
- `alert_events`: alert history with `alert_type`, `channel`, `opportunity_id`, `player`, `market_title`, `roi`, `profit`, `status`, `reason`, and redacted `raw_json`.
- `trades`: dashboard results/P&L table for paper/live result separation.
- `scan_runs`, `opportunities`, `alerts`, `rejected_candidates`: live/cross-market operational history in `data/polylens.db`.

# Services

- `polylens-dashboard.service`: runs `python -m src.cli web-dashboard`; serves the local Command Center on port `8787`.
- `polylens-live-arb.service`: runs the live arbitrage scanner service.
- Future/Phase 5 prop watcher: `deploy/systemd/polylens-prop-watch.service`, intended to run:
  `/home/noel/.venv/bin/python -m src.cli watch-prop-arb --sport basketball_nba --markets player_points --bankroll 1000 --interval 60 --json`

# Environment Variables

Required for sportsbook/player-prop scanning:
- `ODDS_API_KEY`

Required for Telegram alerts:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Dashboard/security:
- `POLYLENS_WEB_HOST` optional host override; default is `127.0.0.1`.
- `POLYLENS_WEB_PASSWORD` optional password gate.

Risk/Kalshi/notification settings are read from environment where applicable. Do not print or store secrets; redaction must cover keys containing `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `PRIVATE`, or `API`.

# Current Dashboard Pages

- Live Opportunities
- Results / P&L
- Operations
- Opportunity Explorer
- Alerts
- Risk Engine
- Bot Control
- Scanner Status
- Alerts / Audit

# Current Known Issues

- Installing or restarting system services may require interactive `sudo`; non-interactive SSH commands can be blocked.
- The prop-watch service file exists but may not be installed in `/etc/systemd/system/` until manual deployment.
- In-app browser automation from Codex has intermittently failed with a Windows sandbox startup error; HTTP checks against `127.0.0.1:8787` have still validated dashboard availability.
- Several files are currently dirty or untracked from prior feature work; avoid committing unrelated changes.

# Next Priorities

- Install and enable `polylens-prop-watch.service` on the host.
- Verify dashboard Bot Control can start/stop/restart/show logs for prop watch after service installation.
- Monitor alert delivery quality, duplicate suppression, and stale scan/alert health.
- Keep paper-trade settlement analytics aligned with real-world result ingestion.
- Continue avoiding live execution until an explicit, separately reviewed live-trading design exists.

# Test Status

Latest known full suite after Phase 5:
- `PYTHONPATH=. /home/noel/.venv/bin/pytest -q`
- Result: `340 passed`

# Important Paths

- CLI: `src/cli.py`
- Web dashboard: `src/web/dashboard.py`, `src/web/controls.py`, `src/web/app.py`
- Prop persistence and analytics: `src/storage/opportunities.py`
- Live/cross-market storage: `src/storage/opportunity_store.py`
- Results service: `src/services/results_service.py`
- Risk engine: `src/risk/`
- Notifications: `src/notifications/`
- Systemd files: `deploy/systemd/`
- Tests: `tests/`
- Databases: `data/polylens.db`, `data/opportunities.db`

Update this file whenever major functionality is added.
