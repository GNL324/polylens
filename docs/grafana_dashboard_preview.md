# Grafana Trader Intelligence — Dashboard Preview

Visual reference for the premium Polylens Trader Intelligence dashboard at [https://dash.noelgrca.com](https://dash.noelgrca.com).

This dashboard is designed for institutional-style operator review: large KPI cards, gate control center, pipeline flow, paper portfolio book, and audit health panels.

## Screenshots

> Placeholder — capture after import on SuperPC Grafana.

| Section | Screenshot |
| --- | --- |
| Executive Overview | `![Executive Overview](screenshots/trader-intelligence-row1.png)` |
| Signal Performance | `![Signal Performance](screenshots/trader-intelligence-row2.png)` |
| Gate Control Center | `![Gate Control Center](screenshots/trader-intelligence-row3.png)` |
| Recommendation Pipeline | `![Recommendation Pipeline](screenshots/trader-intelligence-row4.png)` |
| Paper Portfolio | `![Paper Portfolio](screenshots/trader-intelligence-row5.png)` |
| Trader Intelligence | `![Trader Intelligence](screenshots/trader-intelligence-row6.png)` |
| Audit & Health | `![Audit & Health](screenshots/trader-intelligence-row7.png)` |

## Intended visual layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  HEADER — Polylens Trader Intelligence (read-only · paper-only)             │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 1 — Executive Overview                                                  │
│ [Total Signals] [Validation Accuracy] [Proven Families] [Blocked] [Sim]   │
│     sparkline        sparkline                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 2 — Signal Performance                                                  │
│ [Family Accuracy]  [Family ROI Proxy]  [Validation Count by Family]       │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 3 — Gate Control Center                                                 │
│ [Proven] [Weak] [Unproven] [Top Confidence Gauge]                           │
│ [Gate Confidence Rankings Table]                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 4 — Recommendation Pipeline                                             │
│ Signals → Recommendations → Validation → Gates → Paper Bridge               │
│ [Promoted] [Blocked] [Recommendation Trend Chart]                           │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 5 — Paper Portfolio                                                     │
│ [Candidate] [Simulated] [Blocked]                                           │
│ [Paper Intent Book — notional, source, gate reason]                         │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 6 — Trader Intelligence                                                 │
│ [Top Traders]              [Weak Traders]                                   │
│ [Accuracy Leaderboard]     [Validation Count Leaderboard]                   │
├─────────────────────────────────────────────────────────────────────────────┤
│ ROW 7 — Audit & Health                                                      │
│ [Validation Trend]         [Signal Trend]                                   │
│ [Recommendation Trend]     [Database Health]                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Row-by-row panel guide

### ROW 1 — Executive Overview

Large KPI stat cards with background threshold coloring and sparkline trends where time-series data exists.

| Panel | Data source | Purpose |
| --- | --- | --- |
| Total Signals | `v_dashboard_overview` | Portfolio-scale signal volume |
| Validation Accuracy | `v_dashboard_overview` + `v_validation_trend` sparkline | Overall model quality |
| Proven Signal Families | `v_dashboard_overview` | Families cleared for promotion |
| Blocked Recommendations | `v_dashboard_overview` | Gate-blocked recommendation load |
| Simulated Paper Intents | `v_dashboard_overview` + `v_intent_trend` sparkline | Paper portfolio activity |

### ROW 2 — Signal Performance

Horizontal bar charts ranked by performance, covering all five families:

- `early_entry`
- `conviction`
- `exit`
- `consensus`
- `contrarian`

### ROW 3 — Gate Control Center

Status-colored family counts, top-confidence gauge, and ranked confidence table from `v_gate_confidence`.

### ROW 4 — Recommendation Pipeline

Flow banner plus promoted/blocked KPIs and daily recommendation trend from `v_recommendation_trend`.

### ROW 5 — Paper Portfolio

Intent status KPIs and a paper intent book table with notional USD, signal source, and gate reason from `v_latest_paper_intents`.

### ROW 6 — Trader Intelligence

Dual leaderboard layout for top performers, weak performers, accuracy ranking, and validation depth ranking from `v_trader_leaderboard`.

### ROW 7 — Audit & Health

Long-horizon trend panels plus a database health snapshot from `v_database_health` for operator freshness checks.

## Design principles

- **Institutional density** — information-rich rows without clutter
- **Status semantics** — green proven, amber weak/blocked, red unproven
- **Sparkline context** — KPI cards show recent direction, not just point values
- **Operator-first tables** — sortable, filterable book-style panels
- **Safety by design** — all panels query read-only SQLite views; no execution hooks

## Related assets

- Dashboard JSON: `deploy/grafana/dashboards/polylens-trader-intelligence.json`
- Provisioning guide: `deploy/grafana/provisioning/README.md`
- Setup guide: `docs/grafana_trader_intelligence_dashboard.md`
