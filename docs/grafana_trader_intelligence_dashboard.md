# Grafana Trader Intelligence Dashboard

This guide configures the read-only Polylens Trader Intelligence dashboard on Grafana at [https://dash.noelgrca.com](https://dash.noelgrca.com).

## Prerequisites

- Polylens trader signal pipeline has run at least once (optional; views work on empty databases)
- SQLite database path: `/opt/polylens/data/trader_signals.db`
- Grafana host with plugin installation privileges

## 1. Install SQLite datasource plugin

On the Grafana host:

```bash
grafana-cli plugins install frser-sqlite-datasource
```

## 2. Restart Grafana

```bash
sudo systemctl restart grafana-server
```

Or restart the Grafana container/service used on SuperPC.

## 3. Create datasource

In Grafana:

1. Open **Connections → Data sources → Add data source**
2. Select **SQLite**
3. Configure:
   - **Name:** `Polylens SQLite`
   - **Database path:** `/opt/polylens/data/trader_signals.db`
4. Click **Save & test**

## 4. Initialize dashboard views

On Predix (or any Polylens host with access to the database):

```bash
cd /home/noel/polylens
python3 -m src.cli trader-signal-dashboard-views --json
```

Expected output shape:

```json
{
  "db_path": "data/trader_signals.db",
  "views_created": [
    "v_signal_family_performance",
    "v_gate_status_summary",
    "v_trader_signal_kpis",
    "v_recommendation_pipeline",
    "v_paper_intent_pipeline",
    "v_trader_leaderboard",
    "v_latest_recommendations",
    "v_latest_paper_intents",
    "v_validation_trend"
  ],
  "read_only": true,
  "paper_only": true
}
```

## 5. Import dashboard

1. Open Grafana **Dashboards → New → Import**
2. Upload:

   `deploy/grafana/dashboards/polylens-trader-intelligence.json`

3. Select the **Polylens SQLite** datasource when prompted for `${DS_POLYLENS_SQLITE}`
4. Click **Import**

## 6. Verify dashboard panels

Confirm each section renders without SQL errors:

| Section | Panels |
| --- | --- |
| Overview KPIs | Total Signals, Validated Signals, Proven Families, Blocked Recommendations, Paper Intents |
| Signal Families | Signal Family Performance table |
| Validation Analytics | Accuracy by Signal Type, ROI Proxy by Signal Type, Validation Trend |
| Gates | Proven, Weak, Unproven, Blocked Families, Blocked Family Table |
| Recommendations | Promoted, Blocked, Total Recommendations, Latest Recommendations |
| Paper Bridge | Candidate Intents, Simulated Intents, Blocked Intents, Latest Intents |
| Traders | Top Traders, Weak Traders, Accuracy Rankings |

Empty databases should show zeros and empty tables rather than query failures.

## Dashboard views reference

| View | Purpose |
| --- | --- |
| `v_trader_signal_kpis` | Top-level KPI row |
| `v_signal_family_performance` | Per-family accuracy, rolling windows, gate status |
| `v_gate_status_summary` | Proven / weak / unproven / blocked family counts |
| `v_recommendation_pipeline` | Promoted vs blocked recommendation counts |
| `v_paper_intent_pipeline` | Candidate / simulated / blocked intent counts |
| `v_trader_leaderboard` | Trader accuracy rankings |
| `v_latest_recommendations` | Recent gated recommendations |
| `v_latest_paper_intents` | Recent paper bridge intents |
| `v_validation_trend` | Daily validation accuracy trend |

## Safety statement

This dashboard is:

- **Read-only** — queries SQLite views only; no writes or mutations
- **Paper-only** — surfaces paper bridge intents and simulated recommendations
- **Does not place trades** — no order submission paths are invoked
- **Does not execute orders** — no live execution connectors are used

It is intended for analytics and monitoring only.
