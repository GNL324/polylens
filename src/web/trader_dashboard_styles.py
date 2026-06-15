from __future__ import annotations

TRADER_NAV_ITEMS = ("Overview", "Network", "Profiles", "Signals", "Insights")

OVERVIEW_TABLE_LIMIT = 15

TRADER_TERMINAL_CSS = """
:root {
  --bg: #0F1115;
  --card: #171A21;
  --card-hover: #1C2028;
  --border: #2A2F3A;
  --text: #F5F7FA;
  --text-secondary: #B0B7C3;
  --text-muted: #7D8595;
  --positive: #22C55E;
  --warning: #F59E0B;
  --negative: #EF4444;
  --info: #60A5FA;
}

html, body {
  background: var(--bg) !important;
  color: var(--text) !important;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
}

.q-page, .nicegui-content {
  background: var(--bg) !important;
}

.tt-app {
  max-width: 1600px;
  margin: 0 auto;
  padding: 10px 12px 16px;
  gap: 10px;
}

.tt-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}

.tt-brand-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  letter-spacing: -0.01em;
}

.tt-brand-sub {
  font-size: 11px;
  color: var(--text-muted);
}

.tt-badge {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px 8px;
}

.tt-nav {
  display: flex;
  gap: 18px;
  align-items: center;
}

.tt-nav-btn {
  background: transparent !important;
  color: var(--text-muted) !important;
  box-shadow: none !important;
  min-height: 28px !important;
  padding: 0 2px !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  border-radius: 0 !important;
  border-bottom: 2px solid transparent !important;
}

.tt-nav-btn.active {
  color: var(--text) !important;
  border-bottom-color: var(--text) !important;
}

.tt-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 320px;
  gap: 10px;
  align-items: start;
}

.tt-sidebar, .tt-center, .tt-right {
  min-width: 0;
}

.tt-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
}

.tt-card:hover {
  background: var(--card-hover);
}

.tt-card + .tt-card {
  margin-top: 8px;
}

.tt-section-label {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  margin-bottom: 8px;
  font-weight: 600;
}

.tt-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 8px;
}

.tt-metric-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.tt-metric {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px;
}

.tt-metric .label {
  font-size: 10px;
  color: var(--text-muted);
}

.tt-metric .value {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-top: 2px;
}

.tt-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.tt-kpi {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
}

.tt-kpi .label {
  font-size: 11px;
  color: var(--text-muted);
}

.tt-kpi .value {
  font-size: 24px;
  line-height: 1.1;
  font-weight: 700;
  color: var(--text);
  margin-top: 4px;
}

.tt-kpi .foot {
  font-size: 10px;
  color: var(--text-secondary);
  margin-top: 4px;
}

.tt-kpi .foot.positive { color: var(--positive); }
.tt-kpi .foot.info { color: var(--info); }

.tt-btn-primary {
  background: var(--text) !important;
  color: #0F1115 !important;
  box-shadow: none !important;
  min-height: 30px !important;
  font-size: 12px !important;
  font-weight: 600 !important;
}

.tt-btn-secondary {
  background: transparent !important;
  color: var(--text-secondary) !important;
  border: 1px solid var(--border) !important;
  box-shadow: none !important;
  min-height: 30px !important;
  font-size: 12px !important;
}

.tt-btn-block {
  width: 100%;
}

.tt-stack {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tt-status {
  font-size: 11px;
  color: var(--text-muted);
  min-height: 14px;
}

.tt-input .q-field__control,
.tt-input .q-field__native,
.tt-input input {
  color: var(--text) !important;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
  font-size: 12px !important;
}

.tt-input .q-field__control {
  background: var(--bg) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
  min-height: 34px !important;
}

.tt-input .q-field__label {
  color: var(--text-muted) !important;
  font-size: 11px !important;
}

.tt-table .q-table__top,
.tt-table .q-table__bottom,
.tt-table thead tr,
.tt-table tbody tr,
.tt-table .q-table {
  background: var(--card) !important;
  color: var(--text) !important;
}

.tt-table thead th {
  background: var(--card) !important;
  color: var(--text-secondary) !important;
  font-size: 11px !important;
  font-weight: 600 !important;
  padding: 6px 8px !important;
}

.tt-table tbody td {
  font-size: 12px !important;
  padding: 5px 8px !important;
  border-color: var(--border) !important;
}

.tt-table tbody tr:nth-child(even) {
  background: #14181f !important;
}

.tt-table tbody tr:hover {
  background: var(--card-hover) !important;
}

.tt-table .wallet-cell {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
}

.tt-wallet-panel .metric-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
}

.tt-wallet-panel .metric-row:last-child {
  border-bottom: none;
}

.tt-wallet-panel .metric-key {
  color: var(--text-muted);
}

.tt-wallet-panel .metric-val {
  color: var(--text);
  font-weight: 500;
}

.tt-wallet-panel .metric-val.positive { color: var(--positive); }
.tt-wallet-panel .metric-val.info { color: var(--info); }

.tt-asset-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}

.tt-asset-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px;
}

.tt-asset-card .label {
  font-size: 10px;
  color: var(--text-muted);
}

.tt-asset-card .value {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.tt-center-stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

@media (max-width: 1200px) {
  .tt-layout {
    grid-template-columns: 240px minmax(0, 1fr);
  }
  .tt-right {
    grid-column: 1 / -1;
  }
  .tt-kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .tt-layout {
    grid-template-columns: 1fr;
  }
}
"""
