from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ACCOUNT_HISTORY_PATH = "data/raw/kalshi_account_history.json"


def export_kalshi_account_history(client: Any, output: str | Path = DEFAULT_ACCOUNT_HISTORY_PATH) -> dict[str, Any]:
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "balance": client.get_balance(),
        "positions": client.get_positions(),
        "orders": client.get_orders(),
        "fills": _safe_fills(client),
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "accepted": True,
        "output": str(path),
        "counts": {
            "positions": len(_rows(payload["positions"], "positions")),
            "orders": len(_rows(payload["orders"], "orders")),
            "fills": len(_rows(payload["fills"], "fills")),
        },
    }


def load_kalshi_account_history(path: str | Path = DEFAULT_ACCOUNT_HISTORY_PATH) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"available": False, "reason": f"{target} not found"}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"could not read {target}: {exc}"}
    if not isinstance(payload, dict):
        return {"available": False, "reason": f"{target} did not contain a JSON object"}
    return {"available": True, "path": str(target), "payload": payload}


def _safe_fills(client: Any) -> dict[str, Any]:
    try:
        return client.get_fills()
    except Exception as exc:
        return {"fills": [], "error": str(exc)}


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [row for row in payload[key] if isinstance(row, dict)]
    return []
