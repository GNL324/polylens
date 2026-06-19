"""Dark fintech styling for the Mission Control dashboard."""

MISSION_CONTROL_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {
  --mc-bg: #070b10;
  --mc-bg-2: #0b1118;
  --mc-panel: #111923;
  --mc-panel-2: #131e2a;
  --mc-ink: #f5f8fb;
  --mc-muted: #8ea0b6;
  --mc-border: #26384a;
  --mc-border-soft: #1a2a39;
  --mc-profit: #57d68d;
  --mc-loss: #ff6b78;
  --mc-signal: #5aa7ff;
  --mc-warn: #ffd166;
  --mc-shadow: rgba(0, 0, 0, 0.35);
}

body.mc-body, .mc-body {
  background:
    radial-gradient(circle at 15% 0%, rgba(90, 167, 255, 0.16), transparent 34rem),
    linear-gradient(180deg, var(--mc-bg), var(--mc-bg-2)) !important;
  color: var(--mc-ink) !important;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.mc-shell {
  width: min(100%, 1680px);
  margin: 0 auto;
  padding: 18px 18px 28px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.mc-header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 18px;
  border: 1px solid var(--mc-border);
  background: linear-gradient(135deg, rgba(19, 30, 42, 0.98), rgba(12, 18, 27, 0.98));
  border-radius: 8px;
  padding: 22px;
  box-shadow: 0 18px 48px var(--mc-shadow);
}

.mc-eyebrow {
  color: var(--mc-signal);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.mc-brand h1 {
  margin: 5px 0 4px;
  font-size: clamp(28px, 4vw, 54px);
  line-height: 1;
  font-weight: 800;
  letter-spacing: 0;
}

.mc-brand p {
  margin: 0;
  color: var(--mc-muted);
  font-size: 14px;
}

.mc-header-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 12px;
}

.mc-mode-pill {
  border: 1px solid var(--mc-border);
  border-radius: 999px;
  padding: 9px 15px;
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.mc-mode-pill.paper { color: var(--mc-signal); background: rgba(90, 167, 255, 0.12); }
.mc-mode-pill.live { color: var(--mc-profit); background: rgba(87, 214, 141, 0.12); }

.mc-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.mc-badge {
  border: 1px solid var(--mc-border);
  border-radius: 999px;
  padding: 6px 10px;
  color: var(--mc-muted);
  background: rgba(255, 255, 255, 0.03);
  font-size: 11px;
  font-weight: 700;
}

.mc-badge.active.paper { color: var(--mc-signal); border-color: rgba(90, 167, 255, 0.55); }
.mc-badge.active.live-ready { color: var(--mc-profit); border-color: rgba(87, 214, 141, 0.55); }
.mc-badge.active.live-blocked, .mc-badge.active.kill-switch { color: var(--mc-loss); border-color: rgba(255, 107, 120, 0.65); }
.mc-badge.inactive { opacity: 0.48; }

.mc-tabs {
  width: 100%;
  border: 1px solid var(--mc-border-soft);
  border-radius: 8px;
  background: rgba(17, 25, 35, 0.72);
  overflow-x: auto;
}

.mc-tabs .q-tab {
  color: var(--mc-muted);
  font-weight: 700;
  min-height: 46px;
}

.mc-tabs .q-tab--active {
  color: var(--mc-ink);
}

.mc-tab-panels {
  background: transparent !important;
  color: var(--mc-ink);
}

.mc-tab-panel {
  padding: 0 !important;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.mc-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.mc-grid-2,
.mc-grid-3 {
  display: grid;
  gap: 12px;
}

.mc-grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.mc-grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.mc-grid-2.compact { grid-template-columns: 1fr 1fr; }

.mc-card {
  border: 1px solid var(--mc-border-soft);
  background: linear-gradient(180deg, rgba(19, 30, 42, 0.98), rgba(12, 18, 27, 0.98));
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 14px 34px var(--mc-shadow);
  min-width: 0;
}

.mc-card-title {
  color: var(--mc-muted);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-bottom: 14px;
}

.mc-kpi-card {
  min-height: 118px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.mc-kpi-card .label {
  color: var(--mc-muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

.mc-kpi-card .value {
  color: var(--mc-ink);
  font-size: clamp(20px, 2.1vw, 32px);
  font-weight: 800;
  line-height: 1.08;
  overflow-wrap: anywhere;
}

.profit { color: var(--mc-profit) !important; }
.loss { color: var(--mc-loss) !important; }
.signal { color: var(--mc-signal) !important; }
.warn { color: var(--mc-warn) !important; }

.mc-chart-card {
  min-height: 384px;
}

.mc-rechart {
  width: 100%;
  height: 260px;
}

.mc-market-price {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.mc-market-price span {
  font-size: clamp(24px, 3vw, 40px);
  font-weight: 800;
}

.mc-market-price small {
  font-size: 13px;
  font-weight: 800;
}

.mc-chart-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  color: var(--mc-muted);
  font-size: 12px;
}

.mc-chart-meta strong {
  color: var(--mc-ink);
}

.mc-pipeline {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.mc-stage {
  border: 1px solid var(--mc-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.035);
  padding: 12px;
  min-height: 122px;
}

.mc-stage .name {
  color: var(--mc-muted);
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
}

.mc-stage .status {
  margin: 6px 0;
  font-size: 20px;
  font-weight: 800;
}

.mc-stage.ok .status { color: var(--mc-profit); }
.mc-stage.fail .status { color: var(--mc-loss); }
.mc-stage.warn .status { color: var(--mc-warn); }
.mc-stage.idle .status { color: var(--mc-muted); }

.mc-stage .meta {
  color: var(--mc-muted);
  font-size: 12px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.mc-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.mc-table th,
.mc-table td {
  border-bottom: 1px solid var(--mc-border-soft);
  padding: 9px 6px;
  text-align: left;
  vertical-align: top;
}

.mc-table th {
  color: var(--mc-muted);
  font-size: 11px;
  text-transform: uppercase;
}

.mc-outcome-won { color: var(--mc-profit); font-weight: 800; }
.mc-outcome-lost { color: var(--mc-loss); font-weight: 800; }

.mc-active-bet {
  display: grid;
  gap: 8px;
  color: var(--mc-muted);
  font-size: 14px;
}

.mc-active-bet .market {
  color: var(--mc-ink);
  font-size: 18px;
  font-weight: 800;
  line-height: 1.28;
}

.mc-active-bet .side-up { color: var(--mc-profit); font-weight: 800; }
.mc-active-bet .side-down { color: var(--mc-loss); font-weight: 800; }

.mc-empty {
  color: var(--mc-muted);
  border: 1px dashed var(--mc-border);
  border-radius: 8px;
  padding: 16px;
  font-size: 13px;
}

.mc-metric-list,
.mc-health-list {
  display: grid;
  gap: 8px;
}

.mc-metric-row,
.mc-health-row {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  border-bottom: 1px solid var(--mc-border-soft);
  padding: 8px 0;
  color: var(--mc-muted);
  font-size: 13px;
}

.mc-metric-row strong,
.mc-health-row span:last-child {
  color: var(--mc-ink);
  text-align: right;
}

.mc-health-row .ok { color: var(--mc-profit); }
.mc-health-row .bad { color: var(--mc-loss); }
.mc-health-row .warn { color: var(--mc-warn); }

.mc-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--mc-muted);
  font-size: 12px;
}

.mc-link {
  color: var(--mc-signal);
  font-weight: 800;
  text-decoration: none;
}

@media (max-width: 1200px) {
  .mc-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .mc-grid-2, .mc-grid-3, .mc-grid-2.compact { grid-template-columns: 1fr; }
  .mc-pipeline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 720px) {
  .mc-shell { padding: 12px; }
  .mc-header { grid-template-columns: 1fr; padding: 18px; }
  .mc-header-side { align-items: flex-start; }
  .mc-badges { justify-content: flex-start; }
  .mc-kpi-grid { grid-template-columns: 1fr; }
  .mc-pipeline { grid-template-columns: 1fr; }
  .mc-table { min-width: 620px; }
  .mc-card { overflow-x: auto; }
  .mc-footer { align-items: flex-start; flex-direction: column; gap: 8px; }
}
"""
