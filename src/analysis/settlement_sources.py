from __future__ import annotations

import json
import glob
from pathlib import Path
from typing import Any

from src.analysis.paper_trading_engine import DEFAULT_PAPER_TRADING_DB, init_paper_trading_db
from src.sqlite_utils import closing_connection

DEFAULT_SHORT_CRYPTO_PAPER_DB = "data/short_crypto_paper.db"
DEFAULT_ALERT_DB = "data/polylens.db"
DEFAULT_REPORT_GLOB = "data/reports/*.json"


def get_short_crypto_outcome(
    opportunity_id: str,
    db_path: str | Path = DEFAULT_SHORT_CRYPTO_PAPER_DB,
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"resolved": False}
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "paper_trades" not in tables:
                return {"resolved": False}
            trade = _find_short_crypto_trade(conn, opportunity_id)
            if trade is None:
                return {"resolved": False}
            settlement = None
            if "paper_settlements" in tables:
                settlement = conn.execute("SELECT * FROM paper_settlements WHERE trade_id=? ORDER BY settled_at DESC LIMIT 1", (trade["id"],)).fetchone()
            return _short_crypto_trade_outcome(trade, settlement)
    except Exception:
        return {"resolved": False}


def get_report_outcome(
    opportunity_id: str,
    report_glob: str = DEFAULT_REPORT_GLOB,
) -> dict[str, Any]:
    for path in [Path(item) for item in glob.glob(report_glob)]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        outcome = _find_outcome_in_payload(payload, str(opportunity_id))
        if outcome.get("resolved"):
            outcome["source"] = "report_files"
            outcome["source_path"] = str(path)
            return outcome
    return {"resolved": False}


def get_alert_outcome(
    opportunity_id: str,
    db_path: str | Path = DEFAULT_ALERT_DB,
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"resolved": False}
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "alert_events" not in tables:
                return {"resolved": False}
            rows = conn.execute("SELECT * FROM alert_events ORDER BY created_at DESC").fetchall()
            for row in rows:
                raw = _loads(row["raw_json"])
                candidate = raw.get("candidate") if isinstance(raw, dict) else None
                for payload in (raw, candidate if isinstance(candidate, dict) else {}):
                    if _payload_matches(payload, str(opportunity_id)):
                        outcome = _outcome_from_payload(payload)
                        if outcome.get("resolved"):
                            outcome["source"] = "alerts"
                            return outcome
    except Exception:
        return {"resolved": False}
    return {"resolved": False}


def resolve_opportunity_outcome(opportunity_id: str) -> dict[str, Any]:
    for source, fn in (
        ("short_crypto_paper", get_short_crypto_outcome),
        ("report_files", get_report_outcome),
        ("alerts", get_alert_outcome),
    ):
        outcome = fn(str(opportunity_id))
        if outcome.get("resolved"):
            outcome["source"] = source
            return outcome
    return {"resolved": False}


def settlement_source_audit(
    paper_db_path: str | Path = DEFAULT_PAPER_TRADING_DB,
) -> dict[str, Any]:
    init_paper_trading_db(paper_db_path)
    with closing_connection(paper_db_path) as conn:
        open_positions = conn.execute("SELECT opportunity_id FROM paper_positions WHERE status='open' ORDER BY paper_position_id").fetchall()
    sources = {
        "short_crypto_paper": _short_crypto_resolved_count(),
        "report_files": _report_resolved_count(),
        "alerts": _alert_resolved_count(),
    }
    resolvable = 0
    unresolved = 0
    details = []
    for row in open_positions:
        outcome = resolve_opportunity_outcome(str(row["opportunity_id"]))
        if outcome.get("resolved"):
            resolvable += 1
        else:
            unresolved += 1
        details.append({"opportunity_id": row["opportunity_id"], **outcome})
    return {
        "sources": sources,
        "resolvable_positions": resolvable,
        "unresolved_positions": unresolved,
        "details": details,
    }


def _find_short_crypto_trade(conn: Any, opportunity_id: str) -> Any | None:
    candidates = [opportunity_id]
    try:
        numeric = int(str(opportunity_id))
    except (TypeError, ValueError):
        numeric = None
    predicates = ["id=?", "signal_id=?", "market=?", "ticker=?", "slug=?", "token_id=?"]
    values = [numeric, numeric, opportunity_id, opportunity_id, opportunity_id, opportunity_id]
    for predicate, value in zip(predicates, values):
        if value in (None, ""):
            continue
        row = conn.execute(f"SELECT * FROM paper_trades WHERE {predicate} ORDER BY id DESC LIMIT 1", (value,)).fetchone()
        if row:
            return row
    return None


def _short_crypto_trade_outcome(trade: Any, settlement: Any | None) -> dict[str, Any]:
    status = str(trade["status"] or "").lower()
    raw = _loads(trade["raw_json"])
    settlement_raw = _loads(settlement["raw_json"]) if settlement is not None else {}
    result = str(settlement["result"] if settlement is not None else status).lower()
    if result in {"won", "win"} or status in {"won", "win"}:
        return {"resolved": True, "winner": str(trade["direction"] or settlement_raw.get("resolved_outcome") or raw.get("direction") or "up").lower(), "settlement_price": 1.0}
    if result in {"lost", "loss"} or status in {"lost", "loss"}:
        winner = str(settlement_raw.get("resolved_outcome") or _opposite_direction(trade["direction"]) or "").lower()
        return {"resolved": True, "winner": winner, "settlement_price": 0.0}
    payout = _first_float(settlement_raw, "payout", "settlement_price")
    if payout is not None:
        return {"resolved": True, "winner": str(settlement_raw.get("resolved_outcome") or trade["direction"] or "").lower(), "settlement_price": _bounded_price(payout)}
    return {"resolved": False}


def _find_outcome_in_payload(payload: Any, opportunity_id: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        if _payload_matches(payload, opportunity_id):
            outcome = _outcome_from_payload(payload)
            if outcome.get("resolved"):
                return outcome
        for value in payload.values():
            outcome = _find_outcome_in_payload(value, opportunity_id)
            if outcome.get("resolved"):
                return outcome
    elif isinstance(payload, list):
        for item in payload:
            outcome = _find_outcome_in_payload(item, opportunity_id)
            if outcome.get("resolved"):
                return outcome
    return {"resolved": False}


def _payload_matches(payload: Any, opportunity_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = ("id", "opportunity_id", "opportunity_key", "market_id", "ticker", "slug", "token_id")
    return any(str(payload.get(key)) == str(opportunity_id) for key in keys if payload.get(key) not in (None, ""))


def _outcome_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    explicit = _first_float(payload, "settlement_price", "exit_price", "final_price", "payout")
    winner = payload.get("winner") or payload.get("resolved_outcome") or payload.get("outcome")
    result = str(payload.get("result") or payload.get("status") or "").lower()
    if explicit is not None:
        return {"resolved": True, "winner": str(winner or "").lower(), "settlement_price": _bounded_price(explicit)}
    if winner not in (None, ""):
        return {"resolved": True, "winner": str(winner).lower(), "settlement_price": 1.0}
    if result in {"won", "win"}:
        return {"resolved": True, "winner": "yes", "settlement_price": 1.0}
    if result in {"lost", "loss"}:
        return {"resolved": True, "winner": "no", "settlement_price": 0.0}
    return {"resolved": False}


def _short_crypto_resolved_count(db_path: str | Path = DEFAULT_SHORT_CRYPTO_PAPER_DB) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "paper_trades" not in tables:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status IN ('won','lost','win','loss')").fetchone()[0])
    except Exception:
        return 0


def _report_resolved_count(report_glob: str = DEFAULT_REPORT_GLOB) -> int:
    count = 0
    for path in [Path(item) for item in glob.glob(report_glob)]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        count += _count_resolved_payloads(payload)
    return count


def _alert_resolved_count(db_path: str | Path = DEFAULT_ALERT_DB) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "alert_events" not in tables:
                return 0
            count = 0
            for row in conn.execute("SELECT raw_json FROM alert_events").fetchall():
                count += _count_resolved_payloads(_loads(row["raw_json"]))
            return count
    except Exception:
        return 0


def _count_resolved_payloads(payload: Any) -> int:
    if isinstance(payload, dict):
        count = 1 if _outcome_from_payload(payload).get("resolved") else 0
        return count + sum(_count_resolved_payloads(value) for value in payload.values())
    if isinstance(payload, list):
        return sum(_count_resolved_payloads(item) for item in payload)
    return 0


def _opposite_direction(value: Any) -> str:
    text = str(value or "").lower()
    return {"up": "down", "down": "up", "yes": "no", "no": "yes"}.get(text, "")


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _bounded_price(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _loads(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
