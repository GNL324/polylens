# Grafana Provisioning — Polylens Trader Intelligence

Guide for installing, provisioning, and updating the Polylens Trader Intelligence dashboard on Grafana (SuperPC / `https://dash.noelgrca.com`).

## Overview

| Asset | Path |
| --- | --- |
| Dashboard JSON | `deploy/grafana/dashboards/polylens-trader-intelligence.json` |
| SQLite views initializer | `python3 -m src.cli trader-signal-dashboard-views --json` |
| Database | `/opt/polylens/data/trader_signals.db` |
| Datasource plugin | `frser-sqlite-datasource` |

## 1. Install SQLite datasource plugin

On the Grafana host:

```bash
grafana-cli plugins install frser-sqlite-datasource
sudo systemctl restart grafana-server
```

Verify the plugin appears under **Administration → Plugins**.

## 2. Create SQLite datasource

1. Open **Connections → Data sources → Add data source**
2. Select **SQLite**
3. Configure:

| Field | Value |
| --- | --- |
| Name | `Polylens SQLite` |
| Database path | `/opt/polylens/data/trader_signals.db` |

4. Save & test

The dashboard uses the `${DS_POLYLENS_SQLITE}` variable — map it to this datasource on import.

## 3. Initialize dashboard SQL views

On Predix (or any host with database access):

```bash
cd /home/noel/polylens
python3 -m src.cli trader-signal-dashboard-views --json
```

This creates 14 read-only views including trend and overview layers:

- `v_dashboard_overview`
- `v_signal_family_performance`
- `v_gate_confidence`
- `v_signal_performance_trend`
- `v_recommendation_trend`
- `v_intent_trend`
- `v_database_health`
- (and supporting views)

Views are safe on empty databases.

## 4. Import dashboard

### First import

1. **Dashboards → New → Import**
2. Upload `deploy/grafana/dashboards/polylens-trader-intelligence.json`
3. Select **Polylens SQLite** for `${DS_POLYLENS_SQLITE}`
4. Import

### Re-import after updates

When the dashboard JSON changes in git:

1. Export the current dashboard UID (`polylens-trader-intelligence`) if you have local panel edits to preserve
2. Re-import the updated JSON from the repo
3. Choose **Overwrite existing dashboard**
4. Re-select the SQLite datasource if prompted

Alternatively, use Grafana provisioning (optional advanced setup):

```yaml
# /etc/grafana/provisioning/dashboards/polylens.yaml
apiVersion: 1
providers:
  - name: polylens
    orgId: 1
    folder: Polylens
    type: file
    disableDeletion: false
    updateIntervalSeconds: 300
    options:
      path: /opt/polylens/deploy/grafana/dashboards
```

Copy dashboard JSON to the provisioned path and restart Grafana for automatic reload.

## 5. Verify panels

After import, confirm each row renders without SQL errors:

1. Executive Overview — 5 KPI cards with sparklines
2. Signal Performance — 3 horizontal bar charts
3. Gate Control Center — status stats, gauge, rankings table
4. Recommendation Pipeline — flow banner, KPIs, trend chart
5. Paper Portfolio — intent KPIs and intent book table
6. Trader Intelligence — four leaderboard tables
7. Audit & Health — trend charts and database health table

Empty databases should show zeros and empty charts, not query failures.

## 6. Dashboard updates workflow

```bash
# On Predix after pulling latest main
cd /home/noel/polylens
git pull origin main
python3 -m src.cli trader-signal-dashboard-views --json
# Re-import deploy/grafana/dashboards/polylens-trader-intelligence.json on Grafana
```

## Safety statement

This dashboard is:

- **Read-only** — queries SQLite views only
- **Paper-only** — surfaces simulated intents and recommendations
- **Non-executing** — does not place trades or submit orders
- **Analytics-only** — no live trading connectors

## Related documentation

- `docs/grafana_trader_intelligence_dashboard.md` — original setup guide
- `docs/grafana_dashboard_preview.md` — visual layout and panel descriptions
