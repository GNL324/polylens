from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from src.analysis.wallet_activity import WalletActivityEvent, WalletActivitySource, export_wallet_activity
from src.sqlite_utils import closing_connection

DEFAULT_PAPER_COPY_DB = "data/paper_copy_trader.db"
DEFAULT_PAPER_STAKE = 1.0
DEFAULT_MAX_STAKE_PER_WALLET_PER_RUN = 10.0


class ActivityExporter(Protocol):
    def __call__(
        self,
        wallet: str,
        limit: int | None = None,
        source: WalletActivitySource | None = None,
        store: bool = False,
    ) -> Any:
        ...


@dataclass
class PaperCopyPosition:
    paper_position_id: int
    source_wallet: str
    market_id: str
    market_title: str
    asset: str
    side: str
    entry_timestamp: float
    entry_price: float
    shares: float
    notional: float
    status: str
    exit_timestamp: float | None = None
    exit_price: float | None = None
    pnl: float | None = None
    roi: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def init_paper_copy_db(db_path: str | Path = DEFAULT_PAPER_COPY_DB) -> None:
    with closing_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watched_traders (
                wallet TEXT PRIMARY KEY,
                watched_at TEXT NOT NULL,
                source TEXT NOT NULL,
                alpha_score INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS paper_copy_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                wallets_scanned INTEGER NOT NULL,
                events_seen INTEGER NOT NULL,
                new_events INTEGER NOT NULL,
                copied_trades INTEGER NOT NULL,
                closed_positions INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_copy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                source_wallet TEXT NOT NULL,
                timestamp REAL NOT NULL,
                action TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_title TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                shares REAL NOT NULL,
                amount REAL NOT NULL,
                tx_hash TEXT NOT NULL,
                copied INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES paper_copy_runs(id)
            );
            CREATE TABLE IF NOT EXISTS paper_copy_positions (
                paper_position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_wallet TEXT NOT NULL,
                entry_event_key TEXT NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                market_title TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_timestamp REAL NOT NULL,
                entry_price REAL NOT NULL,
                shares REAL NOT NULL,
                notional REAL NOT NULL,
                status TEXT NOT NULL,
                exit_timestamp REAL,
                exit_price REAL,
                pnl REAL,
                roi REAL
            );
            CREATE TABLE IF NOT EXISTS paper_copy_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                paper_position_id INTEGER NOT NULL,
                source_wallet TEXT NOT NULL,
                exit_event_key TEXT NOT NULL,
                exit_timestamp REAL NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                roi REAL NOT NULL,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES paper_copy_runs(id),
                FOREIGN KEY(paper_position_id) REFERENCES paper_copy_positions(paper_position_id)
            );
            CREATE TABLE IF NOT EXISTS validation_window (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                window_start TEXT NOT NULL,
                baseline_wallets_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_watched_traders_status ON watched_traders(status);
            CREATE INDEX IF NOT EXISTS idx_paper_copy_events_wallet_time ON paper_copy_events(source_wallet, timestamp);
            CREATE INDEX IF NOT EXISTS idx_paper_copy_events_key ON paper_copy_events(event_key);
            CREATE INDEX IF NOT EXISTS idx_paper_copy_positions_wallet_status ON paper_copy_positions(source_wallet, status);
            CREATE INDEX IF NOT EXISTS idx_paper_copy_positions_market_side ON paper_copy_positions(source_wallet, market_id, side, status);
            CREATE INDEX IF NOT EXISTS idx_paper_copy_settlements_wallet ON paper_copy_settlements(source_wallet);
            """
        )


def watch_trader(
    wallet: str,
    *,
    db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    source: str = "manual",
    alpha_score: int = 0,
) -> dict[str, Any]:
    wallet = _normalize_wallet(wallet)
    init_paper_copy_db(db_path)
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO watched_traders (wallet, watched_at, source, alpha_score, status)
            VALUES (?, ?, ?, ?, 'active')
            ON CONFLICT(wallet) DO UPDATE SET
                source=excluded.source,
                alpha_score=excluded.alpha_score,
                status='active'
            """,
            (wallet, _utc_now(), source, int(alpha_score or 0)),
        )
    return {"wallet": wallet, "watched": True, "source": source, "alpha_score": int(alpha_score or 0)}


def load_watched_wallets(db_path: str | Path = DEFAULT_PAPER_COPY_DB) -> list[str]:
    init_paper_copy_db(db_path)
    with closing_connection(db_path) as conn:
        rows = conn.execute("SELECT wallet FROM watched_traders WHERE status='active' ORDER BY wallet").fetchall()
    return [str(row["wallet"]) for row in rows]


def run_paper_copy_trader(
    *,
    db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    wallets: list[str] | None = None,
    limit: int | None = None,
    stake: float = DEFAULT_PAPER_STAKE,
    max_stake_per_wallet: float = DEFAULT_MAX_STAKE_PER_WALLET_PER_RUN,
    exporter: ActivityExporter = export_wallet_activity,
    source: WalletActivitySource | None = None,
) -> dict[str, Any]:
    init_paper_copy_db(db_path)
    watched = [_normalize_wallet(wallet) for wallet in (wallets or load_watched_wallets(db_path))]
    run_id = _create_run(db_path, wallets_scanned=len(watched))
    events_seen = 0
    new_events = 0
    copied_trades = 0
    closed_positions = 0
    by_wallet: dict[str, dict[str, Any]] = {}

    for wallet in watched:
        wallet_summary = by_wallet.setdefault(wallet, {"events_seen": 0, "new_events": 0, "copied_trades": 0, "closed_positions": 0})
        export = exporter(wallet=wallet, limit=limit, source=source, store=False)
        events = list(getattr(export, "events", []) or [])
        events_seen += len(events)
        wallet_summary["events_seen"] += len(events)
        spent = 0.0
        for event in sorted(events, key=lambda item: (_timestamp(item), item.tx_hash, item.action, item.market_id, item.side)):
            event_key = _event_key(event)
            if _event_exists(db_path, event_key):
                continue
            new_events += 1
            wallet_summary["new_events"] += 1
            copied = 0
            if event.action == "buy" and spent + stake <= max_stake_per_wallet:
                if _create_position(db_path, event, event_key, stake):
                    spent += stake
                    copied = 1
                    copied_trades += 1
                    wallet_summary["copied_trades"] += 1
            elif event.action in {"sell", "redeem"}:
                closed = _close_positions(db_path, run_id, event, event_key)
                closed_positions += closed
                wallet_summary["closed_positions"] += closed
            _save_event(db_path, run_id, event, event_key, copied=copied)

    _update_run(db_path, run_id, events_seen=events_seen, new_events=new_events, copied_trades=copied_trades, closed_positions=closed_positions)
    report = paper_copy_report(db_path)
    return {
        "run_id": run_id,
        "wallets_scanned": len(watched),
        "events_seen": events_seen,
        "new_events": new_events,
        "copied_trades": copied_trades,
        "closed_positions": closed_positions,
        "open_positions": report["open_positions"],
        "realized_pnl": report["realized_pnl"],
        "by_wallet": by_wallet,
    }


def paper_copy_report(db_path: str | Path = DEFAULT_PAPER_COPY_DB) -> dict[str, Any]:
    init_paper_copy_db(db_path)
    with closing_connection(db_path) as conn:
        positions = conn.execute("SELECT * FROM paper_copy_positions ORDER BY paper_position_id").fetchall()
    total_positions = len(positions)
    open_positions = [row for row in positions if row["status"] == "open"]
    closed_positions = [row for row in positions if row["status"] == "closed"]
    realized_pnl = round(sum(float(row["pnl"] or 0.0) for row in closed_positions), 6)
    closed_notional = sum(float(row["notional"] or 0.0) for row in closed_positions)
    wins = sum(1 for row in closed_positions if float(row["pnl"] or 0.0) > 0)
    return {
        "copied_trades": total_positions,
        "open_positions": len(open_positions),
        "closed_positions": len(closed_positions),
        "realized_pnl": realized_pnl,
        "roi": round(_ratio(realized_pnl, closed_notional), 6),
        "win_rate": round(_ratio(wins, len(closed_positions)), 6),
        "by_wallet": _report_segment(positions, "source_wallet"),
        "by_asset": _report_segment(positions, "asset"),
    }


def paper_copy_report_by_wallet(
    wallet: str,
    *,
    db_path: str | Path = DEFAULT_PAPER_COPY_DB,
) -> dict[str, Any]:
    """Return paper-copy stats for a single wallet via SQL-level filtering.

    Avoids loading all positions into memory when only one wallet is needed.
    """
    init_paper_copy_db(db_path)
    wallet = str(wallet or "").strip().lower()
    with closing_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS copied_trades,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_positions,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_positions,
                ROUND(COALESCE(SUM(CASE WHEN status = 'closed' THEN COALESCE(pnl, 0.0) ELSE 0.0 END), 0.0), 6) AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN COALESCE(notional, 0.0) ELSE 0.0 END), 0.0) AS closed_notional,
                SUM(CASE WHEN status = 'closed' AND COALESCE(pnl, 0.0) > 0 THEN 1 ELSE 0 END) AS wins
            FROM paper_copy_positions
            WHERE source_wallet = ?
            """,
            (wallet,),
        ).fetchone()
    copied = int(row["copied_trades"] or 0)
    open_pos = int(row["open_positions"] or 0)
    closed_pos = int(row["closed_positions"] or 0)
    realized_pnl = float(row["realized_pnl"] or 0.0)
    closed_notional = float(row["closed_notional"] or 0.0)
    wins = int(row["wins"] or 0)
    return {
        "copied_trades": copied,
        "open_positions": open_pos,
        "closed_positions": closed_pos,
        "realized_pnl": realized_pnl,
        "roi": round(_ratio(realized_pnl, closed_notional), 6),
        "win_rate": round(_ratio(wins, closed_pos), 6),
    }


def start_validation_window(
    db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Snapshot the currently followed wallets as the validation-window baseline cohort.

    Idempotent unless force=True, so re-running setup does not reset an
    already-started window (and its baseline wallet set) by accident.
    """
    init_paper_copy_db(db_path)
    with closing_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT window_start, baseline_wallets_json FROM validation_window WHERE id=1"
        ).fetchone()
        if existing and not force:
            return {
                "started": False,
                "reason": "validation window already started",
                "window_start": existing["window_start"],
                "baseline_wallet_count": len(json.loads(existing["baseline_wallets_json"])),
            }
        baseline = sorted(load_watched_wallets(db_path))
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO validation_window (id, window_start, baseline_wallets_json, created_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                window_start=excluded.window_start,
                baseline_wallets_json=excluded.baseline_wallets_json,
                created_at=excluded.created_at
            """,
            (now, json.dumps(baseline), now),
        )
    return {
        "started": True,
        "window_start": now,
        "baseline_wallet_count": len(baseline),
        "baseline_wallets": baseline,
    }


def load_validation_window(db_path: str | Path = DEFAULT_PAPER_COPY_DB) -> dict[str, Any] | None:
    init_paper_copy_db(db_path)
    with closing_connection(db_path) as conn:
        row = conn.execute(
            "SELECT window_start, baseline_wallets_json, created_at FROM validation_window WHERE id=1"
        ).fetchone()
    if row is None:
        return None
    return {
        "window_start": row["window_start"],
        "baseline_wallets": json.loads(row["baseline_wallets_json"]),
        "created_at": row["created_at"],
    }


def pre_validation_report(
    *,
    db_path: str | Path = DEFAULT_PAPER_COPY_DB,
    signal_db_path: str | Path | None = None,
    traders_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Combined per-wallet sanity report for the pre-validation-window checklist.

    Surfaces win rate, realized P&L, signal count, and edge decay per followed
    wallet, and tags each wallet's `data_status` so an all-zero P&L wallet with
    real closed trades ("zero_pnl_active") can't be mistaken for a wallet that
    simply has no signals yet ("no_signals_yet").
    """
    from src.analysis.trader_registry import DEFAULT_TRADERS_DB
    from src.analysis.trader_signal_engine import DEFAULT_TRADER_SIGNAL_DB, init_trader_signal_db

    signal_db_path = Path(signal_db_path or DEFAULT_TRADER_SIGNAL_DB)
    traders_db_path = Path(traders_db_path or DEFAULT_TRADERS_DB)

    init_paper_copy_db(db_path)
    base_report = paper_copy_report(db_path)
    window = load_validation_window(db_path)
    baseline_wallets = set(window["baseline_wallets"]) if window else set()

    with closing_connection(db_path) as conn:
        watched_rows = conn.execute(
            "SELECT wallet, watched_at, status FROM watched_traders ORDER BY wallet"
        ).fetchall()
        wins_rows = conn.execute(
            """
            SELECT source_wallet, COUNT(*) AS wins
            FROM paper_copy_positions
            WHERE status='closed' AND COALESCE(pnl, 0) > 0
            GROUP BY source_wallet
            """
        ).fetchall()
    watched_meta = {str(row["wallet"]): dict(row) for row in watched_rows}
    wins_by_wallet = {str(row["source_wallet"]): int(row["wins"]) for row in wins_rows}

    signal_counts: dict[str, int] = {}
    if signal_db_path.exists():
        init_trader_signal_db(signal_db_path)
        with closing_connection(signal_db_path) as conn:
            rows = conn.execute("SELECT wallet, COUNT(*) AS signal_count FROM trader_signals GROUP BY wallet").fetchall()
        signal_counts = {str(row["wallet"]): int(row["signal_count"]) for row in rows}

    decay_by_wallet: dict[str, float | None] = {}
    if traders_db_path.exists():
        from src.intelligence.wallet_signal_decay import wallet_decay_report

        decay = wallet_decay_report(
            traders_db_path=str(traders_db_path),
            paper_copy_db_path=str(db_path),
            signal_db_path=str(signal_db_path),
        )
        decay_by_wallet = {row["wallet"]: row["decay_rate"] for row in decay.get("wallets", [])}

    by_wallet_positions = base_report.get("by_wallet", {})
    all_wallets = sorted(set(watched_meta) | set(by_wallet_positions) | set(signal_counts))

    wallet_rows: list[dict[str, Any]] = []
    for wallet in all_wallets:
        meta = watched_meta.get(wallet, {})
        positions = by_wallet_positions.get(wallet, {})
        closed = int(positions.get("closed_positions") or 0)
        copied = int(positions.get("copied_trades") or 0)
        realized_pnl = round(float(positions.get("realized_pnl") or 0.0), 6)
        signal_count = signal_counts.get(wallet, 0)
        win_rate = round(wins_by_wallet.get(wallet, 0) / closed, 6) if closed else None

        if signal_count == 0 and copied == 0:
            data_status = "no_signals_yet"
        elif copied == 0:
            data_status = "signals_no_trades"
        elif closed == 0:
            data_status = "open_only"
        elif realized_pnl == 0.0:
            data_status = "zero_pnl_active"
        else:
            data_status = "active"

        watched_at = meta.get("watched_at")
        cohort = "unassigned"
        cohort_start = watched_at
        if window:
            if wallet in baseline_wallets:
                cohort = "baseline"
                cohort_start = window["window_start"]
            else:
                cohort = "joined_during_window"
                cohort_start = watched_at

        wallet_rows.append(
            {
                "wallet": wallet,
                "status": meta.get("status", "unknown"),
                "cohort": cohort,
                "cohort_start": cohort_start,
                "watched_at": watched_at,
                "signal_count": signal_count,
                "copied_trades": copied,
                "open_positions": int(positions.get("open_positions") or 0),
                "closed_positions": closed,
                "win_rate": win_rate,
                "realized_pnl": realized_pnl,
                "roi": positions.get("roi"),
                "edge_decay_rate": decay_by_wallet.get(wallet),
                "data_status": data_status,
            }
        )

    status_counts: dict[str, int] = {}
    for row in wallet_rows:
        status_counts[row["data_status"]] = status_counts.get(row["data_status"], 0) + 1

    return {
        "read_only": True,
        "paper_only": True,
        "generated_at": _utc_now(),
        "validation_window": window,
        "portfolio": {
            "copied_trades": base_report.get("copied_trades"),
            "open_positions": base_report.get("open_positions"),
            "closed_positions": base_report.get("closed_positions"),
            "realized_pnl": base_report.get("realized_pnl"),
            "roi": base_report.get("roi"),
            "win_rate": base_report.get("win_rate"),
        },
        "wallets": wallet_rows,
        "data_status_counts": status_counts,
    }


def _create_run(db_path: str | Path, wallets_scanned: int) -> int:
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_copy_runs (run_timestamp, wallets_scanned, events_seen, new_events, copied_trades, closed_positions)
            VALUES (?, ?, 0, 0, 0, 0)
            """,
            (_utc_now(), wallets_scanned),
        )
        return int(cur.lastrowid)


def _update_run(db_path: str | Path, run_id: int, *, events_seen: int, new_events: int, copied_trades: int, closed_positions: int) -> None:
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_copy_runs
            SET events_seen=?, new_events=?, copied_trades=?, closed_positions=?
            WHERE id=?
            """,
            (events_seen, new_events, copied_trades, closed_positions, run_id),
        )


def _save_event(db_path: str | Path, run_id: int, event: WalletActivityEvent, event_key: str, *, copied: int) -> None:
    with closing_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO paper_copy_events
            (run_id, event_key, source_wallet, timestamp, action, market_id, market_title, asset, side, price, shares, amount, tx_hash, copied, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_key,
                event.wallet,
                _timestamp(event),
                event.action,
                _market_key(event),
                event.market_title or "unknown",
                (event.asset or "OTHER").upper(),
                event.side or "",
                _safe_float(event.price),
                _safe_float(event.shares),
                _safe_float(event.amount),
                event.tx_hash or "",
                copied,
                json.dumps(event.raw or {}, sort_keys=True),
            ),
        )


def _create_position(db_path: str | Path, event: WalletActivityEvent, event_key: str, stake: float) -> bool:
    price = _safe_float(event.price)
    if price <= 0:
        return False
    shares = stake / price
    with closing_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_copy_positions
            (source_wallet, entry_event_key, market_id, market_title, asset, side, entry_timestamp, entry_price, shares, notional, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                event.wallet,
                event_key,
                _market_key(event),
                event.market_title or "unknown",
                (event.asset or "OTHER").upper(),
                event.side or "",
                _timestamp(event),
                price,
                round(shares, 8),
                round(stake, 6),
            ),
        )
        return cur.rowcount > 0


def _close_positions(db_path: str | Path, run_id: int, event: WalletActivityEvent, event_key: str) -> int:
    exit_price = _exit_price(event)
    if exit_price is None:
        return 0
    market_id = _market_key(event)
    side = event.side or ""
    with closing_connection(db_path) as conn:
        if side:
            rows = conn.execute(
                """
                SELECT * FROM paper_copy_positions
                WHERE source_wallet=? AND market_id=? AND side=? AND status='open'
                ORDER BY entry_timestamp, paper_position_id
                """,
                (event.wallet, market_id, side),
            ).fetchall()
        else:
            candidates = conn.execute(
                """
                SELECT * FROM paper_copy_positions
                WHERE source_wallet=? AND market_id=? AND status='open'
                ORDER BY entry_timestamp, paper_position_id
                """,
                (event.wallet, market_id),
            ).fetchall()
            sides = {str(row["side"] or "") for row in candidates}
            rows = candidates if len(sides) == 1 else []
        closed = 0
        for row in rows:
            notional = float(row["notional"] or 0.0)
            proceeds = float(row["shares"] or 0.0) * exit_price
            pnl = round(proceeds - notional, 6)
            roi = round(_ratio(pnl, notional), 6)
            conn.execute(
                """
                UPDATE paper_copy_positions
                SET status='closed', exit_timestamp=?, exit_price=?, pnl=?, roi=?
                WHERE paper_position_id=?
                """,
                (_timestamp(event), exit_price, pnl, roi, row["paper_position_id"]),
            )
            conn.execute(
                """
                INSERT INTO paper_copy_settlements
                (run_id, paper_position_id, source_wallet, exit_event_key, exit_timestamp, exit_price, pnl, roi, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["paper_position_id"],
                    event.wallet,
                    event_key,
                    _timestamp(event),
                    exit_price,
                    pnl,
                    roi,
                    json.dumps(event.raw or {}, sort_keys=True),
                ),
            )
            closed += 1
        return closed


def _event_exists(db_path: str | Path, event_key: str) -> bool:
    with closing_connection(db_path) as conn:
        row = conn.execute("SELECT 1 FROM paper_copy_events WHERE event_key=?", (event_key,)).fetchone()
    return row is not None


def _report_segment(rows: list[Any], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row[key] or "unknown")
        bucket = buckets.setdefault(label, {"copied_trades": 0, "open_positions": 0, "closed_positions": 0, "realized_pnl": 0.0, "notional": 0.0, "roi": 0.0})
        bucket["copied_trades"] += 1
        if row["status"] == "closed":
            bucket["closed_positions"] += 1
            bucket["realized_pnl"] += float(row["pnl"] or 0.0)
            bucket["notional"] += float(row["notional"] or 0.0)
        else:
            bucket["open_positions"] += 1
    for label, bucket in buckets.items():
        notional = bucket.pop("notional", 0.0)
        bucket["realized_pnl"] = round(bucket["realized_pnl"], 6)
        bucket["roi"] = round(_ratio(bucket["realized_pnl"], notional), 6)
    return dict(sorted(buckets.items()))


def _event_key(event: WalletActivityEvent) -> str:
    return "|".join(
        [
            event.wallet,
            event.tx_hash or "",
            str(_timestamp(event)),
            _market_key(event),
            event.action,
            event.side or "",
            str(_safe_float(event.shares)),
            str(_safe_float(event.price)),
            str(_safe_float(event.amount)),
        ]
    )


def _market_key(event: WalletActivityEvent) -> str:
    return event.market_id or event.condition_id or event.market_slug or event.market_title or "unknown"


def _exit_price(event: WalletActivityEvent) -> float | None:
    price = _safe_float(event.price)
    if price > 0:
        return price
    if event.action == "redeem":
        amount = _safe_float(event.amount)
        shares = _safe_float(event.shares)
        if amount > 0 and shares > 0:
            return amount / shares
        if amount > 0:
            return 1.0
        # A redeem with no payout means the market resolved against this
        # position: it's a real exit at $0, not "no exit data available".
        # Treating it as None left losing positions open forever, which
        # silently biased win rate and realized P&L toward wins only.
        return 0.0
    return None


def _timestamp(event: WalletActivityEvent) -> float:
    value = _safe_float(event.timestamp)
    return value / 1000.0 if value > 10_000_000_000 else value


def _normalize_wallet(wallet: str) -> str:
    wallet = str(wallet or "").strip().lower()
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise ValueError("wallet must be a 0x-prefixed 40 character hex address")
    int(wallet[2:], 16)
    return wallet


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _safe_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
