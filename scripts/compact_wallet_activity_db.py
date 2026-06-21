#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact runaway wallet_activity.db without deleting normalized intelligence.")
    parser.add_argument("--db-path", default="data/wallet_activity.db")
    parser.add_argument("--work-dir", default="/dev/shm/polylens-wallet-activity-compact")
    parser.add_argument("--backup-dir")
    parser.add_argument("--compact-path")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-no-full-backup", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()
    work_dir = Path(args.work_dir).resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    compact_path = Path(args.compact_path).resolve() if args.compact_path else work_dir / f"wallet_activity.compacted.{timestamp}.db"
    manifest_path = db_path.with_suffix(db_path.suffix + f".compact-manifest.{timestamp}.txt")

    if not db_path.exists():
        raise SystemExit(f"missing database: {db_path}")

    source_size = db_path.stat().st_size
    before = inspect_db(db_path)
    print_section("before", before | {"db_bytes": source_size})

    backup_path = None
    if args.backup_dir:
        backup_dir = Path(args.backup_dir).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(backup_dir).free
        if free <= source_size:
            raise SystemExit(f"backup dir {backup_dir} has {free} bytes free, needs > {source_size}")
        backup_path = backup_dir / f"wallet_activity.backup.{timestamp}.db"
        if args.apply:
            print(f"copying full backup to {backup_path}")
            shutil.copy2(db_path, backup_path)
    elif args.apply and not args.allow_no_full_backup:
        raise SystemExit("--apply requires --backup-dir with enough space, or explicit --allow-no-full-backup")

    work_dir.mkdir(parents=True, exist_ok=True)
    if compact_path.exists() and not args.compact_path:
        compact_path.unlink()
    work_free = shutil.disk_usage(work_dir).free
    print(f"work_dir={work_dir} free_bytes={work_free}")
    if args.apply:
        if args.compact_path:
            print(f"using_existing_compact={compact_path}")
        else:
            build_compact_db(db_path, compact_path)
        after = inspect_db(compact_path)
        print_section("compact", after | {"db_bytes": compact_path.stat().st_size})
        validate_compact(before, after)
        manifest = [
            f"created_at={timestamp}",
            f"source_db={db_path}",
            f"source_bytes={source_size}",
            f"compact_db={compact_path}",
            f"compact_bytes={compact_path.stat().st_size}",
            f"full_backup={backup_path or ''}",
            f"allow_no_full_backup={args.allow_no_full_backup}",
            f"wallet_events_before={before['wallet_events']}",
            f"wallet_events_after={after['wallet_events']}",
            f"wallet_exports_before={before['wallet_exports']}",
            f"wallet_exports_after={after['wallet_exports']}",
            f"latest_export_groups={before['wallet_source_groups']}",
        ]
        manifest_path.write_text("\n".join(manifest) + "\n", encoding="utf-8")
        replace_database(db_path, compact_path, keep_retired_until_copy=bool(backup_path))
        final = inspect_db(db_path)
        print_section("final", final | {"db_bytes": db_path.stat().st_size})
        print(f"manifest={manifest_path}")
        if backup_path:
            print(f"rollback: stop services, copy {backup_path} back to {db_path}")
        else:
            print("rollback: compacted DB is now authoritative; no full original backup was created")
    else:
        print("dry_run=true")
        print("rerun with --apply and --backup-dir, or --apply --allow-no-full-backup for critical low-space recovery")
    return 0


def inspect_db(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        exports = int(conn.execute("SELECT COUNT(*) FROM wallet_exports").fetchone()[0])
        events = int(conn.execute("SELECT COUNT(*) FROM wallet_events").fetchone()[0])
        groups = int(conn.execute("SELECT COUNT(*) FROM (SELECT wallet, source FROM wallet_exports GROUP BY wallet, source)").fetchone()[0])
        export_json_bytes = int(conn.execute("SELECT COALESCE(SUM(length(export_json)), 0) FROM wallet_exports").fetchone()[0])
        event_raw_bytes = int(conn.execute("SELECT COALESCE(SUM(length(raw_json)), 0) FROM wallet_events").fetchone()[0])
        return {
            "wallet_exports": exports,
            "wallet_events": events,
            "wallet_source_groups": groups,
            "export_json_bytes": export_json_bytes,
            "event_raw_json_bytes": event_raw_bytes,
        }


def build_compact_db(source: Path, target: Path) -> None:
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as conn:
        conn.execute(f"ATTACH DATABASE ? AS compact", (str(target),))
        conn.executescript(
            """
            PRAGMA compact.journal_mode = OFF;
            PRAGMA compact.synchronous = OFF;
            CREATE TABLE compact.wallet_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                export_timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                export_json TEXT NOT NULL
            );
            CREATE TABLE compact.wallet_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                export_id INTEGER NOT NULL,
                wallet TEXT NOT NULL,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                market_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                market_slug TEXT NOT NULL,
                market_title TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                tx_hash TEXT NOT NULL,
                event_key TEXT NOT NULL UNIQUE,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(export_id) REFERENCES wallet_exports(id)
            );
            INSERT INTO compact.wallet_exports (id, wallet, export_timestamp, source, event_count, export_json)
            SELECT id, wallet, export_timestamp, source, event_count, export_json
            FROM main.wallet_exports
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY wallet, source
                        ORDER BY export_timestamp DESC, id DESC
                    ) AS rn
                    FROM main.wallet_exports
                )
                WHERE rn = 1
            );
            INSERT OR IGNORE INTO compact.wallet_events
                (id, export_id, wallet, timestamp, event_type, market_id, condition_id, market_slug,
                 market_title, asset, side, action, shares, price, amount, tx_hash, event_key, raw_json)
            SELECT
                e.id,
                COALESCE(x.id, (SELECT id FROM compact.wallet_exports WHERE wallet = e.wallet ORDER BY export_timestamp DESC, id DESC LIMIT 1)),
                e.wallet, e.timestamp, e.event_type, e.market_id, e.condition_id, e.market_slug,
                e.market_title, e.asset, e.side, e.action, e.shares, e.price, e.amount, e.tx_hash, e.event_key, e.raw_json
            FROM main.wallet_events e
            LEFT JOIN compact.wallet_exports x ON x.id = e.export_id;
            CREATE INDEX compact.idx_wallet_exports_wallet_time ON wallet_exports(wallet, export_timestamp DESC);
            CREATE UNIQUE INDEX compact.idx_wallet_exports_wallet_source ON wallet_exports(wallet, source);
            CREATE INDEX compact.idx_wallet_events_wallet_time ON wallet_events(wallet, timestamp);
            CREATE INDEX compact.idx_wallet_events_action ON wallet_events(action);
            CREATE INDEX compact.idx_wallet_events_market ON wallet_events(condition_id, market_id);
            PRAGMA compact.integrity_check;
            """
        )
        conn.execute("DETACH DATABASE compact")


def validate_compact(before: dict[str, int], after: dict[str, int]) -> None:
    if after["wallet_events"] != before["wallet_events"]:
        raise SystemExit(f"wallet_events changed: before={before['wallet_events']} after={after['wallet_events']}")
    if after["wallet_exports"] != before["wallet_source_groups"]:
        raise SystemExit(
            f"wallet_exports not compacted to latest group count: groups={before['wallet_source_groups']} after={after['wallet_exports']}"
        )


def replace_database(db_path: Path, compact_path: Path, *, keep_retired_until_copy: bool) -> None:
    retired = db_path.with_suffix(db_path.suffix + ".runaway-retired")
    if retired.exists():
        retired.unlink()
    os.replace(db_path, retired)
    if not keep_retired_until_copy:
        retired.unlink()
        if hasattr(os, "sync"):
            os.sync()
        time.sleep(1)
    try:
        copy_compact_into_place(compact_path, db_path)
    except BaseException:
        if db_path.exists():
            db_path.unlink()
        if retired.exists():
            os.replace(retired, db_path)
        raise
    if retired.exists():
        retired.unlink()
    compact_path.unlink()


def copy_compact_into_place(compact_path: Path, db_path: Path) -> None:
    last_error: BaseException | None = None
    for _ in range(3):
        try:
            shutil.copy2(compact_path, db_path)
            return
        except OSError as exc:
            last_error = exc
            if db_path.exists():
                db_path.unlink()
            if exc.errno != 28:
                raise
            if hasattr(os, "sync"):
                os.sync()
            time.sleep(2)
    if last_error is not None:
        raise last_error


def print_section(name: str, data: dict[str, int]) -> None:
    print(f"== {name} ==")
    for key in sorted(data):
        print(f"{key}={data[key]}")


if __name__ == "__main__":
    raise SystemExit(main())
