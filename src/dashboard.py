from __future__ import annotations

import html
import json
import os
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from src.risk import RiskEngine
from src.storage.opportunities import load_recent_alerts as load_prop_alerts
from src.storage.opportunities import load_recent_opportunities as load_prop_opportunities
from src.storage.opportunity_store import OpportunityStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DB_PATH = "data/polylens.db"
DEFAULT_PROP_DB_PATH = "data/opportunities.db"


def run_dashboard(host: str | None = None, port: int | None = None, db_path: str = DEFAULT_DB_PATH) -> None:
    host = host or os.environ.get("POLYLENS_DASHBOARD_HOST", DEFAULT_HOST)
    port = int(port or os.environ.get("POLYLENS_DASHBOARD_PORT", DEFAULT_PORT))
    prop_db_path = os.environ.get("POLYLENS_PROP_DB_PATH", DEFAULT_PROP_DB_PATH)

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/", "/index.html"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = render_dashboard(db_path=db_path, prop_db_path=prop_db_path).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            data = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
            engine = RiskEngine(db_path)
            if self.path == "/halt":
                engine.halt(data.get("reason", ["dashboard emergency halt"])[0] or "dashboard emergency halt")
            elif self.path == "/resume":
                engine.resume()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Polylens dashboard running at http://{host}:{port}")
    server.serve_forever()


def render_dashboard(db_path: str = DEFAULT_DB_PATH, prop_db_path: str = DEFAULT_PROP_DB_PATH) -> str:
    engine = RiskEngine(db_path)
    status = engine.status()
    risk_events = engine.recent_events(limit=10)
    smoke_events = engine.recent_smoke_test_events(limit=10)
    main_store = OpportunityStore(db_path)
    latest_opportunities = main_store.recent_opportunities(limit=10)
    latest_alerts = main_store.recent_alerts(limit=10)
    latest_prop_opportunities = _safe(lambda: load_prop_opportunities(limit=10, db_path=prop_db_path), [])
    latest_prop_alerts = _safe(lambda: load_prop_alerts(limit=10, db_path=prop_db_path), [])
    scan_runs = _recent_scan_runs(db_path)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polylens Dashboard</title>
  <style>
    :root {{ color-scheme: light; font-family: Arial, sans-serif; --line: #d7dde5; --ink: #17202a; --muted: #5f6b7a; --bg: #f6f8fa; --panel: #fff; --accent: #176b87; --danger: #a92020; }}
    body {{ margin: 0; color: var(--ink); background: var(--bg); }}
    header {{ background: #10212b; color: #fff; padding: 18px 24px; display: flex; justify-content: space-between; gap: 16px; align-items: center; flex-wrap: wrap; }}
    h1 {{ margin: 0; font-size: 22px; }}
    main {{ padding: 18px; display: grid; gap: 16px; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; overflow-x: auto; }}
    h2 {{ margin: 0 0 10px; font-size: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: #fbfcfd; }}
    .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    .value {{ font-size: 20px; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    form {{ display: inline-flex; gap: 8px; margin-right: 8px; }}
    input {{ padding: 8px; border: 1px solid var(--line); border-radius: 4px; min-width: 220px; }}
    button, .button {{ padding: 8px 10px; border: 1px solid var(--line); border-radius: 4px; background: #fff; color: var(--ink); cursor: pointer; text-decoration: none; }}
    .danger {{ background: var(--danger); color: #fff; border-color: var(--danger); }}
    .pill {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: #e7f0f3; color: var(--accent); font-size: 12px; }}
    .halt {{ background: #fdecec; color: var(--danger); }}
    pre {{ white-space: pre-wrap; margin: 0; font-size: 12px; }}
  </style>
</head>
<body>
  <header>
    <h1>Polylens</h1>
    <div><span class="pill">{_e(status["mode"])}</span> <span class="pill {'halt' if status['global_halt'] else ''}">Global halt: {_e(status['global_halt'])}</span></div>
  </header>
  <main>
    <section>
      <h2>Controls</h2>
      <form method="post" action="/halt"><input name="reason" value="dashboard emergency halt"><button class="danger" type="submit">Emergency Halt</button></form>
      <form method="post" action="/resume"><button type="submit">Resume</button></form>
      <a class="button" href="/">Refresh</a>
    </section>
    <section class="grid">
      {_metric("Mode", status["mode"])}
      {_metric("Dry Run", status["dry_run"])}
      {_metric("Live Trading", f"{status['live_trading']} (blocked)")}
      {_metric("Daily PnL", status["state"].get("daily_pnl", 0))}
      {_metric("Monthly PnL", status["state"].get("monthly_pnl", 0))}
      {_metric("Venue Halts", len(status["venue_halts"]))}
    </section>
    <section><h2>Venue Halts</h2>{_table(status["venue_halts"], ["timestamp", "venue", "reason"])}</section>
    <section><h2>Exposure By Venue</h2>{_dict_table(status["exposure_by_venue"], "venue", "exposure")}</section>
    <section><h2>Exposure By Market</h2>{_dict_table(status["exposure_by_market"], "market", "exposure")}</section>
    <section><h2>Latest Opportunities</h2>{_table(latest_opportunities + latest_prop_opportunities, ["timestamp", "venue_pair", "kalshi_ticker", "player", "raw_edge", "execution_score", "ranking_score"])}</section>
    <section><h2>Latest Alerts</h2>{_table(latest_alerts + latest_prop_alerts, ["sent_timestamp", "timestamp", "destination", "status", "opportunity_id", "opportunity_key", "error"])}</section>
    <section><h2>Latest Smoke-Test Events</h2>{_table(smoke_events, ["timestamp", "decision", "venue", "market", "reason", "proposed_stake", "metadata"])}</section>
    <section><h2>Latest Risk Rejections</h2>{_table([r for r in risk_events if r.get("decision") != "APPROVE"], ["timestamp", "decision", "venue", "market", "reason", "score", "edge", "proposed_stake"])}</section>
    <section><h2>Recent Scan Runs</h2>{_table(scan_runs, ["timestamp", "scan_mode", "candidate_count", "alert_count"])}</section>
  </main>
</body>
</html>"""


def _recent_scan_runs(db_path: str, limit: int = 10) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, scan_mode, candidate_count, alert_count FROM scan_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _metric(label: str, value: Any) -> str:
    return f'<div class="metric"><div class="label">{_e(label)}</div><div class="value">{_e(value)}</div></div>'


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "<p>No rows.</p>"
    body = []
    for row in rows:
        cells = "".join(f"<td>{_format(row.get(column))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    headers = "".join(f"<th>{_e(column)}</th>" for column in columns)
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _dict_table(values: dict[str, Any], key_label: str, value_label: str) -> str:
    rows = [{key_label: key, value_label: value} for key, value in values.items()]
    return _table(rows, [key_label, value_label])


def _format(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return f"<pre>{_e(json.dumps(value, sort_keys=True, default=str))}</pre>"
    return _e(value)


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))
