# Local Dashboard

Polylens includes a local-only dashboard for monitoring opportunities, alerts, risk state, and halt controls.

Start it with:

```bash
python -m src.cli dashboard
```

By default it binds to `127.0.0.1:8765`.

Configuration:

```text
POLYLENS_DASHBOARD_HOST   Default: 127.0.0.1
POLYLENS_DASHBOARD_PORT   Default: 8765
POLYLENS_PROP_DB_PATH     Default: data/opportunities.db
```

CLI flags can also set host, port, and the main DB path:

```bash
python -m src.cli dashboard --host 127.0.0.1 --port 8765 --db-path data/polylens.db
```

The dashboard shows:

- Current mode: `DRY RUN`, `PAPER`, or `LIVE BLOCKED`
- Global and venue halt status
- Latest stored opportunities
- Latest stored alerts
- Latest risk rejections
- Daily and monthly PnL placeholders from risk state
- Exposure by venue and market
- Recent scan runs

Controls:

- Emergency Halt creates a global risk halt in SQLite.
- Resume clears active global risk halts.
- Refresh reloads the page.

The dashboard is intended for local operator use only. It does not add authentication and should not be bound to a public interface.
