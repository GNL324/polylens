from __future__ import annotations

from pathlib import Path
from typing import Any

from src.services.results_service import fetch_trades, initialize_results_store, summarize_results


def results_payload(db_path: str | Path = "data/polylens.db") -> dict[str, Any]:
    initialize_results_store(db_path)
    return {"summary": summarize_results(db_path), "trades": fetch_trades(db_path)}

