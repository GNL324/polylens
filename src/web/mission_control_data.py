from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from src.analysis.short_crypto_executor import ShortCryptoRiskConfig
from src.analysis.short_crypto_paper import (
    DEFAULT_DB_PATH as SHORT_CRYPTO_PAPER_DB_PATH,
    ShortCryptoPaperStore,
    fetch_spot_prices,
    performance_report,
)
from src.web.db import POLYLENS_DB_PATH, connect, table_exists

DEFAULT_PAPER_EXPOSURE = 100.0
REFRESH_SECONDS = 8


def mission_control_snapshot(
    *,
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    polylens_db_path: str | Path = POLYLENS_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    report = performance_report(str(paper_db_path), now_ts=now_ts)
    store = ShortCryptoPaperStore(str(paper_db_path))
    open_exposure = store.open_exposure()
    latest_run = _latest_paper_run(paper_db_path)
    mode = _mode_badges()
    risk = _risk_panel()
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "header": _header(latest_run, mode, now, open_exposure),
        "kpis": _kpis(report, paper_db_path, open_exposure, now, now_ts),
        "pnl_chart": pnl_series_24h(paper_db_path, now=now),
        "btc_feed": btc_feed(paper_db_path, now=now),
        "pipeline": pipeline_stages(paper_db_path, now=now),
        "active_bet": active_bet(paper_db_path, now_ts=now_ts),
        "recent_settlements": recent_settlements(paper_db_path, limit=12),
        "risk": risk,
        "system_health": system_health(paper_db_path, polylens_db_path, now=now),
        "mode_badges": mode,
    }


def _header(
    latest_run: dict[str, Any] | None,
    mode: dict[str, bool],
    now: datetime,
    open_exposure: float,
) -> dict[str, Any]:
    meta = _load_json(latest_run.get("raw_json") if latest_run else None) or {}
    filters = meta.get("strategy_filters") or {}
    strategy_label = (
        meta.get("strategy_label")
        or filters.get("strategy_label")
        or os.environ.get("POLYLENS_PAPER_STRATEGY_LABEL")
        or "short-crypto"
    )
    venues = (latest_run or {}).get("venues") or os.environ.get("POLYLENS_PAPER_VENUES") or "polymarket"
    mode_label = "LIVE" if mode.get("live_ready") and not mode.get("paper") else "PAPER"
    return {
        "title": "POLYLENS",
        "mode_label": mode_label,
        "utc_time": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "wallet": f"${open_exposure:.2f} deployed",
        "venue": str(venues).split(",")[0].strip(),
        "strategy_label": str(strategy_label),
    }


def _kpis(
    report: dict[str, Any],
    paper_db_path: str | Path,
    open_exposure: float,
    now: datetime,
    now_ts: float,
) -> dict[str, Any]:
    max_exposure = _float_env("POLYLENS_PAPER_MAX_EXPOSURE", DEFAULT_PAPER_EXPOSURE)
    daily_loss_limit = ShortCryptoRiskConfig.from_env().max_daily_loss
    daily_loss = _daily_realized_loss(paper_db_path, now)
    closed = int(report.get("closed_trades") or 0)
    return {
        "realized_pnl": float(report.get("realized_pnl") or 0.0),
        "unrealized_pnl": float(report.get("unrealized_pnl") or 0.0),
        "total_trades": int(report.get("total_trades") or 0),
        "win_rate": report.get("win_rate"),
        "trades_per_day": trades_per_day(paper_db_path, now=now),
        "current_streak": compute_streak(paper_db_path),
        "daily_loss_remaining": max(0.0, daily_loss_limit - daily_loss),
        "bankroll": max_exposure,
        "available_cash": max(0.0, max_exposure - open_exposure),
        "open_trades": int(report.get("open_trades") or 0),
        "closed_trades": closed,
    }


def pnl_series_24h(
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    rows = _settlements_since(paper_db_path, cutoff)
    points: list[dict[str, Any]] = []
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for row in rows:
        pnl = float(row.get("pnl") or 0.0)
        cumulative += pnl
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
        points.append(
            {
                "ts": row.get("settled_at"),
                "pnl": round(cumulative, 4),
                "trade_pnl": pnl,
            }
        )
    current = cumulative
    if not points:
        points = [{"ts": now.isoformat().replace("+00:00", "Z"), "pnl": 0.0, "trade_pnl": 0.0}]
    return {
        "points": points,
        "peak": round(peak, 4),
        "drawdown": round(drawdown, 4),
        "current_pnl": round(current, 4),
        "svg": render_line_svg(points, value_key="pnl", height=140, stroke="#1f7a3f"),
    }


def btc_feed(
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    price = None
    change_1h_pct = None
    try:
        prices = fetch_spot_prices(["BTC"])
        price = prices.get("BTC")
    except Exception:
        price = None
    stats = _coinbase_stats("BTC-USD")
    if price is None and stats:
        price = stats.get("last")
    if stats and stats.get("open") and price is not None:
        try:
            open_price = float(stats["open"])
            if open_price:
                change_1h_pct = ((float(price) - open_price) / open_price) * 100.0
        except (TypeError, ValueError):
            change_1h_pct = None

    markers = _btc_trade_markers(paper_db_path, now=now)
    chart_points = _btc_chart_points(markers, price, now=now)
    return {
        "price": price,
        "change_1h_pct": change_1h_pct,
        "markers": markers,
        "points": chart_points,
        "svg": render_line_svg(chart_points, value_key="price", height=140, stroke="#1d4f91", markers=markers),
    }


def pipeline_stages(
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    latest_run = _latest_paper_run(paper_db_path)
    if not latest_run:
        return [_idle_stage(name) for name in ("Scan", "Predict", "Validate", "Size", "Fill", "Settle")]

    meta = _load_json(latest_run.get("raw_json")) or {}
    run_id = int(latest_run.get("id") or meta.get("run_id") or 0)
    latency_ms = _run_latency_ms(latest_run)
    errors = meta.get("errors") or []
    last_error = _short_error(errors[-1] if errors else None)
    rejected = int(meta.get("rejected_signals") or latest_run.get("rejected_signals") or 0)
    created = int(meta.get("paper_trades_created") or latest_run.get("created_trades") or 0)
    markets_seen = int(meta.get("markets_seen") or 0)
    signal_stats = _run_signal_stats(paper_db_path, run_id)

    settle_info = _latest_settlement_info(paper_db_path)

    scan_ok = markets_seen > 0 and not errors
    scan_state = "ok" if scan_ok else ("fail" if errors else "idle")

    predict_count = signal_stats["total"]
    predict_ok = predict_count > 0
    predict_state = "ok" if predict_ok else ("idle" if markets_seen == 0 else "warn")

    validate_ok = rejected >= 0 and (signal_stats["accepted"] > 0 or rejected > 0 or markets_seen > 0)
    validate_state = "ok" if validate_ok else "idle"
    validate_error = signal_stats.get("last_rejection")

    size_blocks = int(meta.get("exposure_blocks") or 0)
    size_ok = size_blocks == 0 or created > 0
    size_state = "ok" if size_ok and markets_seen else ("warn" if size_blocks else "idle")

    fill_ok = created > 0
    fill_state = "ok" if fill_ok else ("warn" if rejected else "idle")

    settle_ok = settle_info is not None
    settle_state = "ok" if settle_ok else "idle"

    return [
        _stage("Scan", scan_state, latency_ms, last_error if scan_state == "fail" else None, f"{markets_seen} markets"),
        _stage("Predict", predict_state, latency_ms, None, f"{predict_count} signals"),
        _stage("Validate", validate_state, latency_ms, validate_error, f"{rejected} rejected"),
        _stage("Size", size_state, latency_ms, "exposure cap" if size_blocks else None, f"{size_blocks} blocks"),
        _stage("Fill", fill_state, latency_ms, None, f"{created} fills"),
        _stage("Settle", settle_state, settle_info.get("latency_ms") if settle_info else latency_ms, None, settle_info.get("label") if settle_info else "no settlements"),
    ]


def active_bet(
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    *,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    now_ts = now_ts if now_ts is not None else time.time()
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_trades"):
            return None
        row = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    trade = dict(row)
    raw = _load_json(trade.get("raw_json")) or {}
    end_ts = _parse_ts(trade.get("end_time"))
    countdown = _format_countdown(end_ts, now_ts) if end_ts else "—"
    market_title = raw.get("slug") or trade.get("slug") or trade.get("market") or trade.get("ticker") or "BTC short market"
    direction = str(trade.get("direction") or trade.get("side") or "").upper()
    mark_value = _trade_mark_value(trade, raw)
    return {
        "market_title": str(market_title),
        "side": direction,
        "entry_price": float(trade.get("entry_price") or 0.0),
        "size": float(trade.get("size") or 0.0),
        "expected_edge": float(trade.get("edge") or 0.0),
        "expiry_countdown": countdown,
        "mark_value": mark_value,
        "strategy_label": trade.get("strategy_label") or raw.get("strategy_label"),
    }


def recent_settlements(
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_settlements") or not table_exists(conn, "paper_trades"):
            return []
        rows = conn.execute(
            """
            SELECT
                ps.settled_at,
                ps.result,
                ps.pnl,
                ps.settlement_source,
                pt.market,
                pt.slug,
                pt.ticker,
                pt.direction,
                pt.strategy_label,
                pt.raw_json AS trade_raw_json
            FROM paper_settlements ps
            JOIN paper_trades pt ON pt.id = ps.trade_id
            ORDER BY ps.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = _load_json(item.pop("trade_raw_json", None)) or {}
        market = item.get("slug") or item.get("ticker") or item.get("market") or "—"
        results.append(
            {
                "settled_at": item.get("settled_at"),
                "result": str(item.get("result") or ""),
                "pnl": float(item.get("pnl") or 0.0),
                "market": str(market),
                "reason": item.get("settlement_source") or raw.get("strategy_label") or "—",
                "model_label": item.get("strategy_label") or raw.get("strategy_label") or "—",
                "direction": str(item.get("direction") or "").upper(),
            }
        )
    return results


def _risk_panel() -> dict[str, Any]:
    config = ShortCryptoRiskConfig.from_env()
    kill_active = Path(config.kill_switch).exists()
    live_flags = {
        "POLYLENS_LIVE_TRADING": _env_true("POLYLENS_LIVE_TRADING"),
        "POLYLENS_AUTONOMOUS_CRYPTO": _env_true("POLYLENS_AUTONOMOUS_CRYPTO"),
        "POLYLENS_CONFIRM_RISK_ACK": _env_true("POLYLENS_CONFIRM_RISK_ACK"),
    }
    kalshi_send = _env_true("POLYLENS_KALSHI_LIVE_SENDS_ENABLED")
    poly_send = _env_true("POLYLENS_POLYMARKET_LIVE_SENDS_ENABLED")
    live_send_gate = (not kalshi_send and not poly_send) or (kalshi_send or poly_send)
    return {
        "kill_switch": kill_active,
        "kill_switch_path": config.kill_switch,
        "max_stake": config.max_trade_stake,
        "daily_loss_limit": config.max_daily_loss,
        "duplicate_suppression": config.duplicate_trade_protection,
        "stale_data_guard_secs": config.data_freshness_seconds,
        "liquidity_guard_min": config.min_liquidity,
        "close_cutoff_secs": config.close_cutoff_seconds,
        "live_send_gate": {
            "kalshi": kalshi_send,
            "polymarket": poly_send,
            "open": live_send_gate,
        },
        "live_flags": live_flags,
    }


def system_health(
    paper_db_path: str | Path,
    polylens_db_path: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return {
        "short_crypto_timer": _systemd_state("polylens-short-crypto-paper.timer"),
        "short_crypto_settle_timer": _systemd_state("polylens-short-crypto-paper-settle.timer"),
        "dashboard_service": _systemd_state("polylens-dashboard.service"),
        "telegram_console_service": _systemd_state("polylens-telegram-console.service"),
        "telegram_console_health_timer": _systemd_state("polylens-telegram-console-health.timer"),
        "last_scan_time": _last_paper_run_time(paper_db_path),
        "paper_db_path": str(Path(paper_db_path).resolve()),
        "polylens_db_path": str(Path(polylens_db_path).resolve()),
        "api_connectivity": _api_connectivity(),
        "refreshed_at": now.isoformat().replace("+00:00", "Z"),
    }


def _mode_badges() -> dict[str, bool]:
    config = ShortCryptoRiskConfig.from_env()
    kill_active = Path(config.kill_switch).exists()
    live_flags = [
        _env_true("POLYLENS_LIVE_TRADING"),
        _env_true("POLYLENS_AUTONOMOUS_CRYPTO"),
        _env_true("POLYLENS_CONFIRM_RISK_ACK"),
    ]
    live_enabled = all(live_flags)
    paper_mode = not live_enabled
    live_ready = live_enabled and not kill_active
    live_blocked = live_enabled and kill_active
    return {
        "paper": paper_mode,
        "live_ready": live_ready,
        "live_blocked": live_blocked or (live_enabled and not live_ready),
        "kill_switch": kill_active,
    }


def compute_streak(paper_db_path: str | Path) -> dict[str, Any]:
    rows = _closed_results_chronological(paper_db_path)
    if not rows:
        return {"type": "none", "count": 0}
    latest = str(rows[-1].get("result") or "")
    if latest not in {"won", "lost"}:
        return {"type": "none", "count": 0}
    count = 0
    for row in reversed(rows):
        if str(row.get("result") or "") != latest:
            break
        count += 1
    return {"type": latest, "count": count}


def trades_per_day(
    paper_db_path: str | Path,
    *,
    now: datetime | None = None,
    days: int = 7,
) -> float | None:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_trades"):
            return None
        count = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE created_at >= ?",
            (start.isoformat().replace("+00:00", "Z"),),
        ).fetchone()[0]
    return round(float(count) / float(days), 2)


def format_pnl(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}${value:.2f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def format_price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def render_line_svg(
    points: list[dict[str, Any]],
    *,
    value_key: str,
    height: int = 140,
    width: int = 640,
    stroke: str = "#1f7a3f",
    markers: list[dict[str, Any]] | None = None,
) -> str:
    if not points:
        return f'<svg class="mc-chart-svg" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg"></svg>'
    values = [float(p.get(value_key) or 0.0) for p in points]
    min_v = min(values)
    max_v = max(values)
    if min_v == max_v:
        min_v -= 1.0
        max_v += 1.0
    pad = 12
    inner_w = width - pad * 2
    inner_h = height - pad * 2

    def x_at(i: int) -> float:
        if len(points) == 1:
            return width / 2
        return pad + (i / (len(points) - 1)) * inner_w

    def y_at(v: float) -> float:
        ratio = (v - min_v) / (max_v - min_v)
        return pad + inner_h - ratio * inner_h

    path_parts = []
    for i, v in enumerate(values):
        cmd = "M" if i == 0 else "L"
        path_parts.append(f"{cmd}{x_at(i):.2f},{y_at(v):.2f}")
    path = " ".join(path_parts)
    zero_y = y_at(0.0) if min_v <= 0 <= max_v else None
    zero_line = (
        f'<line x1="{pad}" y1="{zero_y:.2f}" x2="{width - pad}" y2="{zero_y:.2f}" stroke="#d8d2c4" stroke-dasharray="3 3"/>'
        if zero_y is not None
        else ""
    )
    marker_svg = ""
    if markers:
        for marker in markers:
            idx = marker.get("index")
            if idx is None or idx < 0 or idx >= len(points):
                continue
            cx = x_at(int(idx))
            cy = y_at(float(points[int(idx)].get(value_key) or 0.0))
            color = "#1f7a3f" if marker.get("direction") == "up" else "#b3261e"
            marker_svg += f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="3.5" fill="{color}" stroke="#1a1f16" stroke-width="1"/>'
    return (
        f'<svg class="mc-chart-svg" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#faf7f0"/>'
        f"{zero_line}"
        f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="2"/>'
        f"{marker_svg}"
        f"</svg>"
    )


def _latest_paper_run(paper_db_path: str | Path) -> dict[str, Any] | None:
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_runs"):
            return None
        row = conn.execute("SELECT * FROM paper_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _last_paper_run_time(paper_db_path: str | Path) -> str | None:
    run = _latest_paper_run(paper_db_path)
    if not run:
        return None
    return str(run.get("completed_at") or run.get("started_at") or "")


def _run_latency_ms(run: dict[str, Any]) -> int | None:
    start = _parse_ts(run.get("started_at"))
    end = _parse_ts(run.get("completed_at"))
    if start is None or end is None:
        return None
    return max(0, int((end - start) * 1000))


def _run_signal_stats(paper_db_path: str | Path, run_id: int) -> dict[str, Any]:
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_signals"):
            return {"total": 0, "accepted": 0, "rejected": 0, "last_rejection": None}
        total = conn.execute("SELECT COUNT(*) FROM paper_signals WHERE run_id = ?", (run_id,)).fetchone()[0]
        accepted = conn.execute(
            "SELECT COUNT(*) FROM paper_signals WHERE run_id = ? AND status = 'accepted'",
            (run_id,),
        ).fetchone()[0]
        rejected = conn.execute(
            "SELECT COUNT(*) FROM paper_signals WHERE run_id = ? AND status = 'rejected'",
            (run_id,),
        ).fetchone()[0]
        last = conn.execute(
            """
            SELECT rejection_reason FROM paper_signals
            WHERE run_id = ? AND status = 'rejected' AND rejection_reason IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    return {
        "total": int(total),
        "accepted": int(accepted),
        "rejected": int(rejected),
        "last_rejection": str(last[0]) if last and last[0] else None,
    }


def _latest_settlement_info(paper_db_path: str | Path) -> dict[str, Any] | None:
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_settlements"):
            return None
        row = conn.execute(
            "SELECT settled_at, result FROM paper_settlements ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    settled_at = _parse_ts(row[0])
    return {
        "label": f"last {row[1]}",
        "latency_ms": None,
        "settled_at": row[0],
        "settled_ts": settled_at,
    }


def _settlements_since(paper_db_path: str | Path, cutoff: datetime) -> list[dict[str, Any]]:
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_settlements"):
            return []
        rows = conn.execute(
            """
            SELECT settled_at, pnl, result
            FROM paper_settlements
            WHERE settled_at >= ?
            ORDER BY settled_at ASC
            """,
            (cutoff_text,),
        ).fetchall()
    return [dict(row) for row in rows]


def _closed_results_chronological(paper_db_path: str | Path) -> list[dict[str, Any]]:
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_settlements"):
            return []
        rows = conn.execute(
            """
            SELECT result, settled_at
            FROM paper_settlements
            WHERE result IN ('won', 'lost')
            ORDER BY settled_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _daily_realized_loss(paper_db_path: str | Path, now: datetime) -> float:
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    cutoff = day_start.isoformat().replace("+00:00", "Z")
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_settlements"):
            return 0.0
        rows = conn.execute(
            "SELECT pnl FROM paper_settlements WHERE settled_at >= ?",
            (cutoff,),
        ).fetchall()
    loss = 0.0
    for row in rows:
        pnl = float(row[0] or 0.0)
        if pnl < 0:
            loss += abs(pnl)
    return loss


def _btc_trade_markers(paper_db_path: str | Path, *, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=1)
    cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
    with connect(paper_db_path) as conn:
        if not table_exists(conn, "paper_trades"):
            return []
        rows = conn.execute(
            """
            SELECT created_at, direction, raw_json
            FROM paper_trades
            WHERE created_at >= ?
            ORDER BY created_at ASC
            """,
            (cutoff_text,),
        ).fetchall()
    markers: list[dict[str, Any]] = []
    for row in rows:
        raw = _load_json(row[2]) or {}
        spot = raw.get("spot_price")
        if spot is None:
            continue
        markers.append(
            {
                "ts": row[0],
                "direction": str(row[1] or "").lower(),
                "price": float(spot),
            }
        )
    return markers


def _btc_chart_points(
    markers: list[dict[str, Any]],
    current_price: float | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    points = [{"price": m["price"]} for m in markers]
    if current_price is not None:
        points.append({"price": float(current_price)})
    if not points and current_price is not None:
        points = [{"price": float(current_price)}]
    for i, marker in enumerate(markers):
        marker["index"] = i
    return points


def _trade_mark_value(trade: dict[str, Any], raw: dict[str, Any]) -> float | None:
    try:
        model = float(trade.get("model_probability") or 0.0)
        size = float(trade.get("size") or 0.0)
        entry = float(trade.get("entry_price") or 0.0)
        return (model - entry) * size
    except (TypeError, ValueError):
        pass
    if raw.get("expected_value") is not None:
        try:
            return float(raw["expected_value"])
        except (TypeError, ValueError):
            return None
    return None


def _coinbase_stats(symbol: str) -> dict[str, Any] | None:
    try:
        req = Request(
            f"https://api.exchange.coinbase.com/products/{symbol}/stats",
            headers={"User-Agent": "polylens/0.1", "Accept": "application/json"},
        )
        with urlopen(req, timeout=4) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _api_connectivity() -> dict[str, str]:
    kalshi = "unknown"
    polymarket = "unknown"
    try:
        from src.adapters.kalshi import KalshiClient

        client = KalshiClient()
        client.get_exchange_status()
        kalshi = "ok"
    except Exception:
        kalshi = "error"
    try:
        from src.adapters.polymarket import PolymarketClient

        client = PolymarketClient()
        client.get_markets(limit=1)
        polymarket = "ok"
    except Exception:
        polymarket = "error"
    return {"kalshi": kalshi, "polymarket": polymarket}


def _systemd_state(unit: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        text = (result.stdout or result.stderr or "").strip().lower()
        return text or "unknown"
    except Exception:
        return "unknown"


def _stage(name: str, state: str, latency_ms: int | None, error: str | None, detail: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "status": {"ok": "OK", "fail": "FAIL", "warn": "WARN", "idle": "IDLE"}.get(state, state.upper()),
        "latency_ms": latency_ms,
        "last_error": _short_error(error),
        "detail": detail,
    }


def _idle_stage(name: str) -> dict[str, Any]:
    return _stage(name, "idle", None, None, "no runs")


def _short_error(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        text = value.get("error") or value.get("message") or value.get("reason") or json.dumps(value, default=str)
    else:
        text = str(value)
    text = text.strip()
    if len(text) > 80:
        return text[:77] + "..."
    return text or None


def _format_countdown(end_ts: float, now_ts: float) -> str:
    remaining = max(0, int(end_ts - now_ts))
    minutes, seconds = divmod(remaining, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes:02d}m {seconds:02d}s"


def _parse_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _load_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _env_true(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


_legacy_mission_control_snapshot = mission_control_snapshot


def _top_opportunity_row(
    row: dict[str, Any],
    *,
    portfolio_db_path: str | Path,
    now: datetime | None,
) -> dict[str, Any]:
    from src.analysis.decision_intelligence import explain_opportunity

    explanation = explain_opportunity(row, scoring_v3=row.get("scoring_v3"), db_path=portfolio_db_path, now=now)
    return {
        "rank": row.get("rank"),
        "score": row.get("final_score") or row.get("ranking_score"),
        "expected_edge": row.get("expected_edge"),
        "confidence": row.get("confidence"),
        "expected_execution_quality": row.get("expected_execution_quality"),
        "projected_risk": row.get("projected_risk"),
        "explanation_summary": row.get("explanation_summary"),
        "decision_summary": explanation.get("summary"),
        "decision": explanation.get("decision"),
        "title": row.get("title"),
        "strategy": row.get("strategy"),
        "market_id": row.get("market_id"),
    }


def mission_control_snapshot(
    *,
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    polylens_db_path: str | Path = POLYLENS_DB_PATH,
    portfolio_db_path: str | Path = "data/paper_trading.db",
    now: datetime | None = None,
) -> dict[str, Any]:
    from src.analysis.opportunity_feed import get_paper_trading_opportunities
    from src.analysis.outcome_validator import outcome_validation_report
    from src.analysis.signal_weight_report import build_signal_weight_report, mission_control_section
    from src.analysis.paper_trading_engine import performance_report as canonical_paper_performance
    from src.analysis.paper_execution_analytics import execution_quality_report

    snapshot = _legacy_mission_control_snapshot(
        paper_db_path=paper_db_path,
        polylens_db_path=polylens_db_path,
        now=now,
    )
    portfolio = canonical_paper_performance(portfolio_db_path)
    kpis = snapshot["kpis"]
    kpis.update(
        {
            "starting_bankroll": portfolio["starting_bankroll"],
            "current_equity": portfolio["total_equity"],
            "cash_balance": portfolio["cash_balance"],
            "open_position_value": portfolio["open_position_value"],
            "realized_pnl": portfolio["realized_pnl"],
            "unrealized_pnl": portfolio["unrealized_pnl"],
            "total_pnl": portfolio["total_pnl"],
            "roi_pct": portfolio["roi_pct"],
            "total_trades": portfolio["trade_count"],
            "open_trades": portfolio["open_positions"],
            "closed_trades": portfolio["closed_positions"],
            "win_rate": portfolio["win_rate"],
            "bankroll": portfolio["starting_bankroll"],
            "available_cash": portfolio["cash_balance"],
            "exposure": portfolio.get("exposure", 0.0),
            "today_return": portfolio.get("today_return", 0.0),
            "weekly_return": portfolio.get("weekly_return", 0.0),
            "monthly_return": portfolio.get("monthly_return", 0.0),
            "drawdown": portfolio.get("drawdown", 0.0),
            "max_drawdown": portfolio.get("max_drawdown", 0.0),
            "execution_quality": execution_quality_report(portfolio_db_path),
        }
    )
    snapshot["top_opportunities"] = [
        _top_opportunity_row(row, portfolio_db_path=portfolio_db_path, now=now)
        for row in get_paper_trading_opportunities(
            limit=10,
            portfolio_db_path=portfolio_db_path,
            include_auxiliary=False,
            now=now,
        )
    ]
    snapshot["header"]["wallet"] = f"${portfolio['open_position_value']:.2f} deployed"
    equity_points = [
        {"ts": row.get("timestamp"), "equity": float(row.get("equity") or 0.0)}
        for row in portfolio.get("equity_curve", [])
    ]
    if not equity_points:
        equity_points = [{"ts": snapshot["generated_at"], "equity": float(portfolio["total_equity"])}]
    snapshot["paper_portfolio"] = {
        "equity_curve": {
            "points": equity_points,
            "svg": render_line_svg(equity_points, value_key="equity", height=140, stroke="#1f7a3f"),
        },
        "cash": portfolio["cash_balance"],
        "exposure": portfolio.get("exposure", 0.0),
        "today_return": portfolio.get("today_return", 0.0),
        "weekly_return": portfolio.get("weekly_return", 0.0),
        "monthly_return": portfolio.get("monthly_return", 0.0),
        "drawdown": portfolio.get("drawdown", 0.0),
        "max_drawdown": portfolio.get("max_drawdown", 0.0),
        "best_strategy": portfolio.get("best_strategy"),
        "worst_strategy": portfolio.get("worst_strategy"),
        "best_wallet": portfolio.get("best_wallet"),
        "worst_wallet": portfolio.get("worst_wallet"),
    }
    snapshot["outcome_validation"] = outcome_validation_report(portfolio_db_path, now=now)
    weight_report = build_signal_weight_report(portfolio_db_path, now=now)
    snapshot["signal_weight_optimization"] = mission_control_section(weight_report)
    return snapshot



_canonical_mission_control_snapshot = mission_control_snapshot


def mission_control_snapshot(
    *,
    paper_db_path: str | Path = SHORT_CRYPTO_PAPER_DB_PATH,
    polylens_db_path: str | Path = POLYLENS_DB_PATH,
    portfolio_db_path: str | Path = "data/paper_trading.db",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not Path(portfolio_db_path).exists():
        return _legacy_mission_control_snapshot(
            paper_db_path=paper_db_path,
            polylens_db_path=polylens_db_path,
            now=now,
        )
    return _canonical_mission_control_snapshot(
        paper_db_path=paper_db_path,
        polylens_db_path=polylens_db_path,
        portfolio_db_path=portfolio_db_path,
        now=now,
    )
