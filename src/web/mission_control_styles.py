"""Retro terminal styling for the Mission Control dashboard."""

MISSION_CONTROL_CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Press+Start+2P&display=swap');

:root {
  --mc-bg: #f4f0e6;
  --mc-panel: #faf7f0;
  --mc-ink: #1a1f16;
  --mc-muted: #5c6358;
  --mc-border: #2a3328;
  --mc-profit: #1f7a3f;
  --mc-loss: #b3261e;
  --mc-signal: #1d4f91;
  --mc-warn: #8a6d1d;
  --mc-shadow: rgba(26, 31, 22, 0.08);
}

body.mc-body, .mc-body {
  background: var(--mc-bg) !important;
  color: var(--mc-ink) !important;
  font-family: 'IBM Plex Mono', ui-monospace, monospace;
}

.mc-shell {
  max-width: 1680px;
  margin: 0 auto;
  padding: 12px 14px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.mc-header {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px 16px;
  border: 1px solid var(--mc-border);
  background: var(--mc-panel);
  padding: 10px 12px;
  box-shadow: 2px 2px 0 var(--mc-shadow);
}

.mc-title {
  font-family: 'Press Start 2P', 'IBM Plex Mono', monospace;
  font-size: 11px;
  letter-spacing: 0.04em;
  line-height: 1.6;
}

.mc-subtitle {
  font-size: 11px;
  color: var(--mc-muted);
  text-transform: uppercase;
}

.mc-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
  align-items: center;
}

.mc-badge {
  border: 1px solid var(--mc-border);
  padding: 3px 8px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  background: #fff;
}

.mc-badge.paper { color: var(--mc-signal); }
.mc-badge.live-ready { color: var(--mc-profit); background: #e8f6ec; }
.mc-badge.live-blocked { color: var(--mc-loss); background: #fdecea; }
.mc-badge.kill-switch { color: #fff; background: var(--mc-loss); }

.mc-grid-hero {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  gap: 8px;
}

.mc-grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.mc-grid-3 {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr;
  gap: 8px;
}

.mc-grid-bottom {
  display: grid;
  grid-template-columns: 1.1fr 1fr 1fr;
  gap: 8px;
}

.mc-card {
  border: 1px solid var(--mc-border);
  background: var(--mc-panel);
  padding: 8px 10px;
  box-shadow: 2px 2px 0 var(--mc-shadow);
  min-width: 0;
}

.mc-card-title {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--mc-muted);
  margin-bottom: 6px;
  border-bottom: 1px dashed var(--mc-border);
  padding-bottom: 4px;
}

.mc-kpi {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 58px;
}

.mc-kpi .label {
  font-size: 9px;
  text-transform: uppercase;
  color: var(--mc-muted);
  letter-spacing: 0.06em;
}

.mc-kpi .value {
  font-family: 'Press Start 2P', 'IBM Plex Mono', monospace;
  font-size: 13px;
  line-height: 1.4;
  word-break: break-word;
}

.mc-kpi .value.profit { color: var(--mc-profit); }
.mc-kpi .value.loss { color: var(--mc-loss); }
.mc-kpi .value.signal { color: var(--mc-signal); }

.mc-chart-wrap {
  width: 100%;
  overflow: hidden;
}

.mc-chart-svg {
  width: 100%;
  height: 140px;
  display: block;
}

.mc-chart-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 10px;
  color: var(--mc-muted);
  margin-top: 4px;
}

.mc-chart-meta strong {
  color: var(--mc-ink);
}

.mc-pipeline {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 6px;
}

.mc-stage {
  border: 1px solid var(--mc-border);
  background: #fff;
  padding: 6px;
  min-height: 92px;
}

.mc-stage .name {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 4px;
}

.mc-stage .status {
  font-size: 11px;
  font-weight: 600;
}

.mc-stage.ok .status { color: var(--mc-profit); }
.mc-stage.fail .status { color: var(--mc-loss); }
.mc-stage.idle .status { color: var(--mc-muted); }
.mc-stage.warn .status { color: var(--mc-warn); }

.mc-stage .meta {
  font-size: 9px;
  color: var(--mc-muted);
  margin-top: 4px;
  line-height: 1.35;
  word-break: break-word;
}

.mc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
}

.mc-table th,
.mc-table td {
  border-bottom: 1px solid #d8d2c4;
  padding: 4px 3px;
  text-align: left;
  vertical-align: top;
}

.mc-table th {
  color: var(--mc-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 600;
}

.mc-outcome-won { color: var(--mc-profit); font-weight: 600; }
.mc-outcome-lost { color: var(--mc-loss); font-weight: 600; }

.mc-risk-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.mc-risk-item {
  border: 1px dashed #c9c2b2;
  padding: 5px 6px;
  font-size: 10px;
}

.mc-risk-item .label {
  color: var(--mc-muted);
  text-transform: uppercase;
  font-size: 9px;
}

.mc-risk-item .value {
  margin-top: 2px;
  font-weight: 600;
}

.mc-health-list {
  display: grid;
  gap: 4px;
  font-size: 10px;
}

.mc-health-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px dotted #d8d2c4;
  padding-bottom: 3px;
}

.mc-health-row .ok { color: var(--mc-profit); }
.mc-health-row .bad { color: var(--mc-loss); }
.mc-health-row .warn { color: var(--mc-warn); }

.mc-active-bet {
  display: grid;
  gap: 4px;
  font-size: 11px;
}

.mc-active-bet .market {
  font-weight: 600;
  line-height: 1.35;
}

.mc-active-bet .side-up { color: var(--mc-profit); }
.mc-active-bet .side-down { color: var(--mc-loss); }

.mc-empty {
  color: var(--mc-muted);
  font-size: 10px;
  font-style: italic;
}

.mc-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 9px;
  color: var(--mc-muted);
  padding-top: 4px;
}

.mc-link {
  color: var(--mc-signal);
  text-decoration: none;
  font-size: 10px;
}

@media (max-width: 1200px) {
  .mc-grid-hero { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .mc-grid-3, .mc-grid-bottom, .mc-grid-2 { grid-template-columns: 1fr; }
  .mc-pipeline { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
"""
