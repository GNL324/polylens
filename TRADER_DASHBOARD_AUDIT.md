# Trader Intelligence Dashboard Audit

**Audit date:** 2026-06-15 UTC  
**Repository:** https://git.noelgrca.com/Noel-Lab/polylens.git  
**Branch:** `main`  
**Commit:** `44a56fb09ef21cd31fa14d4f79c6eb867b256acf`  
**Host reviewed:** Predix (`192.168.68.62`)

---

## 1. Architecture Role

`src/web/trader_dashboard.py` implements a dedicated **Trader Intelligence Center** NiceGUI dashboard. It is a separate web application from the main Polylens Command Center (`src/web/app.py` / `src/web/dashboard.py`).

Key characteristics:
- Binds to `127.0.0.1:8788` by default (`DEFAULT_TRADER_DASHBOARD_HOST`, `DEFAULT_TRADER_DASHBOARD_PORT`).
- Exports `run_trader_dashboard(host, port)` and `create_trader_dashboard()`.
- Registered in `src/cli.py` as the `trader-dashboard` subcommand.
- Navigation items: Overview, Network, Profiles, Signals, Discovery, Performance, Service, Insights.
- Loads data from existing Wallet Intelligence modules:
  - `WalletDiscoveryEngine`
  - `wallet_discovery_analytics_report`
  - `wallet_performance_analytics_report`
  - `wallet_signal_analytics_report`
  - `wallet_service_health_summary`
  - `WalletAutonomyService` / `load_wallet_autonomy_reports`
- All underlying modules are read-only and paper-only.

It is **not** integrated into the main Command Center (`http://127.0.0.1:8787/`). It is a sibling dashboard with a different UI (`TRADER_TERMINAL_CSS`) and a different data focus.

---

## 2. Current Deployment State

| Item | State |
|---|---|
| `polylens-dashboard.service` (main dashboard on `8787`) | ✅ Active and running |
| `wallet-autonomy.service` + `.timer` | ✅ Active and running |
| `polylens-trader-signal-cycle.timer` | ✅ Active and running |
| `trader-dashboard` CLI command | ✅ Registered in `src/cli.py` |
| Trader Intelligence Dashboard process | ❌ Not running |
| `polylens-trader-dashboard.service` | ❌ Does not exist |
| `polylens-trader-dashboard.timer` | ❌ Does not exist |
| Port `8788` listener | ❌ None |

Verified on Predix:

```text
$ ss -tlnp | grep 8788
(no output)

$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8788/
000
```

---

## 3. Superseded by Mission Control?

**No.** Mission Control (`/mission-control` on port `8787`) is a separate view within the main Polylens dashboard. It focuses on live opportunities, P&L, risk engine, scanner status, and bot control.

The Trader Intelligence Dashboard is a distinct product surface for:
- Wallet discovery
- Wallet scoring and ranking
- Wallet performance analytics
- Wallet signal analytics
- Wallet autonomy service health
- Trader network / profiles / insights

There is no evidence in `src/web/mission_control.py` or `src/web/dashboard.py` that these wallet-intelligence views were migrated into the main dashboard. Therefore, the Trader Intelligence Dashboard is **not superseded** — it is simply **not deployed**.

---

## 4. Is a systemd Unit Missing?

**Yes.** There is no systemd service or timer for the Trader Intelligence Dashboard in either:
- `/home/noel/polylens/deploy/systemd/`
- `/etc/systemd/system/` on Predix

The CLI command exists, but there is no persistent process definition. The dashboard is currently only runnable manually:

```bash
cd /home/noel/polylens
PYTHONPATH=. /home/noel/.venv/bin/python -m src.cli trader-dashboard --host 127.0.0.1 --port 8788
```

---

## 5. Should It Be Exposed as a Service?

**Recommendation: deploy it as a systemd service, but bind it to localhost and access it via SSH tunnel or reverse proxy with authentication.**

Rationale:
- The dashboard provides valuable wallet-intelligence visibility.
- It is read-only and paper-only, so operational risk is low.
- It already has its own port, CLI command, and data sources.
- Other Polylens web-facing components (`polylens-dashboard.service`) are already deployed as systemd services.
- It should **not** be bound to `0.0.0.0` without authentication, because NiceGUI does not provide built-in auth and it would expose wallet analytics to the network.

---

## 6. Recommended Deployment Plan

### Step 1 — Create unit file

File: `deploy/systemd/polylens-trader-dashboard.service`

```ini
[Unit]
Description=Polylens Trader Intelligence Center (read-only wallet analytics)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=noel
WorkingDirectory=/home/noel/polylens
Environment=PYTHONPATH=/home/noel/polylens
ExecStart=/home/noel/.venv/bin/python -m src.cli trader-dashboard --host 127.0.0.1 --port 8788
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Step 2 — Install and enable on Predix

```bash
cd /home/noel/polylens
sudo cp deploy/systemd/polylens-trader-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polylens-trader-dashboard.service
```

### Step 3 — Verify

```bash
systemctl status polylens-trader-dashboard.service --no-pager
ss -tlnp | grep 8788
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8788/
```

### Step 4 — Remote access (if needed)

Since the dashboard binds to `127.0.0.1:8788`, access it from another host via SSH tunnel:

```bash
ssh -L 8788:127.0.0.1:8788 -N noel@predix
```

Then open `http://127.0.0.1:8788/` on the local machine.

Alternatively, expose it behind an authenticated reverse proxy (e.g., nginx with basic auth or Tailscale serve) and never bind it to `0.0.0.0` directly.

### Step 5 — SRE monitoring update

Add `polylens-trader-dashboard.service` to the SRE health review:
- Check process / port `8788` availability.
- Include it in `systemctl --failed` checks.
- Alert if the service stops (but only if it has been deployed).

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Dashboard exposed without auth | Bind to `127.0.0.1`; use SSH tunnel or authenticated reverse proxy |
| Resource contention with main dashboard on `8787` | Separate port, separate process, minimal expected load |
| Stale data if Wallet Autonomy Service stops | Already monitored by SRE cron job |
| Live trading concern | Dashboard is read-only and paper-only; no order paths |

---

## 8. Final Recommendation

**Deploy `polylens-trader-dashboard.service` on Predix.**

The Trader Intelligence Dashboard is a complete, read-only, paper-only application that is currently dormant. It should be brought online as a localhost-bound systemd service and accessed via SSH tunnel or authenticated proxy. It should not be auto-deployed without your explicit approval, and the deployment plan above is ready for execution.

Do not proceed with deployment unless explicitly approved.
