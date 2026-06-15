from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.analysis.trader_discovery import (
    DEFAULT_TRADER_DISCOVERY_DB,
    load_discovered_wallets,
)
from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR
from src.intelligence.wallet_data_acquisition import run_wallet_acquisition
from src.intelligence.wallet_seed_import import WalletSeedImporter, bootstrap_health_report
from src.intelligence.wallet_synthetic_filter import (
    filter_production_wallets,
    is_synthetic_wallet,
    partition_wallets,
    wallet_production_flags,
)
from src.sqlite_utils import closing_connection

DEFAULT_REAL_SEED_WALLETS_PATH = "data/traders/real_seed_wallets.json"


def _with_flags(payload: dict[str, Any]) -> dict[str, Any]:
    return {"read_only": True, "paper_only": True, "real_wallet_only": True, **payload}


def load_real_seed_entries(path: str | Path = DEFAULT_REAL_SEED_WALLETS_PATH) -> list[dict[str, Any]]:
    seed_path = Path(path)
    if not seed_path.exists():
        return []
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        entries = payload.get("wallets") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        return []
    results: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            results.append({"wallet": entry, "source": "real_seed", "confidence": 0.5})
        elif isinstance(entry, dict):
            results.append(dict(entry))
    return [row for row in results if not is_synthetic_wallet(str(row.get("wallet") or ""))]


def discover_real_wallet_exports(export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR) -> list[Path]:
    root = Path(export_dir)
    if not root.exists():
        return []
    exports: list[Path] = []
    for path in sorted(root.glob("*_activity.json")):
        wallet = path.stem.replace("_activity", "")
        if not is_synthetic_wallet(wallet):
            exports.append(path)
    return exports


def real_wallet_inventory(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    real_seed_path: str | Path = DEFAULT_REAL_SEED_WALLETS_PATH,
    export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
) -> dict[str, Any]:
    registry_wallets = [row.wallet for row in list_traders(limit=10_000, db_path=str(traders_db_path))]
    discovery_wallets = [row.wallet for row in load_discovered_wallets(db_path=discovery_db_path, limit=10_000)]
    seed_wallets = [str(row.get("wallet") or "") for row in load_real_seed_entries(real_seed_path)]
    export_wallets = [path.stem.replace("_activity", "") for path in discover_real_wallet_exports(export_dir)]
    all_wallets = registry_wallets + discovery_wallets + seed_wallets + export_wallets
    real, synthetic = partition_wallets(all_wallets)
    return _with_flags(
        {
            "real_wallet_count": len(set(real)),
            "synthetic_wallet_count": len(set(synthetic)),
            "registry_real_count": len(filter_production_wallets(registry_wallets)),
            "discovery_real_count": len(filter_production_wallets(discovery_wallets)),
            "real_seed_count": len(seed_wallets),
            "real_export_count": len(export_wallets),
            "synthetic_wallets": sorted(set(synthetic)),
        }
    )


def run_real_wallet_ingestion(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    real_seed_path: str | Path = DEFAULT_REAL_SEED_WALLETS_PATH,
    export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    limit: int = 100,
) -> dict[str, Any]:
    importer = WalletSeedImporter(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        export_dir=export_dir,
        seed_wallets_path=real_seed_path,
        seed_exports_dir="data/traders/seed_exports",
    )
    seed_entries = load_real_seed_entries(real_seed_path)
    seed_imports: list[dict[str, Any]] = []
    for entry in seed_entries:
        wallet = str(entry.get("wallet") or "")
        export_path = entry.get("export_path") or entry.get("first_seen_source")
        if export_path and Path(str(export_path)).exists():
            result = importer.import_wallet_export(export_path, source=str(entry.get("source") or "real_seed"))
        else:
            report_payload = {
                "wallet": wallet,
                "classification": entry.get("classification") or "unknown",
                "confidence": float(entry.get("confidence") or 0.5),
                "metrics": entry.get("metrics") or {},
                "signals": entry.get("signals") or [],
            }
            result = importer._materialize_report(
                report_payload,
                source=str(entry.get("source") or "real_seed"),
                confidence=float(entry.get("confidence") or 0.5),
            )
        seed_imports.append(result.to_dict())

    export_imports: list[dict[str, Any]] = []
    for path in discover_real_wallet_exports(export_dir):
        result = importer.import_wallet_export(path, source="real_export")
        export_imports.append(result.to_dict())

    for path in sorted(Path("data/traders/seed_exports").glob("*.json")):
        wallet_hint = json.loads(path.read_text(encoding="utf-8")).get("wallet", "")
        if is_synthetic_wallet(str(wallet_hint)):
            continue
        result = importer.import_forensic_report(path, source="real_seed_export")
        seed_imports.append(result.to_dict())

    acquisition = run_wallet_acquisition(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        limit=limit,
    )
    inventory = real_wallet_inventory(
        traders_db_path=traders_db_path,
        discovery_db_path=discovery_db_path,
        real_seed_path=real_seed_path,
        export_dir=export_dir,
    )
    health = bootstrap_health_report(traders_db_path=traders_db_path, discovery_db_path=discovery_db_path)
    imported = [row for row in seed_imports + export_imports if row.get("imported")]
    return _with_flags(
        {
            "seed_imports": seed_imports,
            "export_imports": export_imports,
            "imported_count": len(imported),
            "acquisition": acquisition,
            "inventory": inventory,
            "health": health,
        }
    )


def synthetic_rejection_stats(
    *,
    traders_db_path: str | Path = DEFAULT_TRADERS_DB,
    days: int = 7,
) -> dict[str, Any]:
    with closing_connection(traders_db_path) as conn:
        rows = conn.execute(
            """
            SELECT source, COUNT(*) AS count
            FROM wallet_acquisition_records
            WHERE status = 'rejected'
              AND reason LIKE '%synthetic%'
              AND recorded_at >= datetime('now', ?)
            GROUP BY source
            """,
            (f"-{max(int(days), 1)} days",),
        ).fetchall()
        total = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM wallet_acquisition_records
            WHERE status = 'rejected'
              AND reason LIKE '%synthetic%'
              AND recorded_at >= datetime('now', ?)
            """,
            (f"-{max(int(days), 1)} days",),
        ).fetchone()["count"]
    return {
        "synthetic_rejections": int(total or 0),
        "by_source": {str(row["source"]): int(row["count"]) for row in rows},
    }
