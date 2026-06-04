from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


DEFAULT_REPORT_JSON = "data/reports/opportunity_rankings.json"
DEFAULT_REPORT_CSV = "data/reports/opportunity_rankings.csv"
DEFAULT_DB_PATHS = ("data/polylens.db", "data/opportunities.db")
DEFAULT_SCAN_GLOBS = ("data/reports/*.json",)


def rank_opportunities(
    *,
    venue: str | None = None,
    market_type: str | None = None,
    asset: str | None = None,
    sport: str | None = None,
    min_roi: float | None = None,
    min_confidence: float | None = None,
    max_age_seconds: int | None = None,
    limit: int = 20,
    export: bool = False,
    db_paths: tuple[str, ...] = DEFAULT_DB_PATHS,
    scan_globs: tuple[str, ...] = DEFAULT_SCAN_GLOBS,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    raw = _load_all_candidates(db_paths, scan_globs)
    normalized = [_normalize_candidate(item, now) for item in raw]
    filtered = [
        row
        for row in normalized
        if _passes_filters(row, venue=venue, market_type=market_type, asset=asset, sport=sport, min_roi=min_roi, min_confidence=min_confidence, max_age_seconds=max_age_seconds)
    ]
    ranked = sorted(filtered, key=lambda row: row["ranking_score"], reverse=True)
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    limited = ranked[: max(int(limit), 0)]
    result = {
        "summary": {
            "status": "ranked" if limited else "no_opportunities",
            "raw_candidates": len(raw),
            "candidates_after_filtering": len(filtered),
            "returned": len(limited),
            "best_opportunity_category": _best_category(limited),
            "data_quality_warnings": _data_warnings(raw, normalized),
            "real_trading_decision_ready": _decision_ready(limited),
        },
        "filters": {
            "venue": venue,
            "market_type": market_type,
            "asset": asset,
            "sport": sport,
            "min_roi": min_roi,
            "min_confidence": min_confidence,
            "max_age_seconds": max_age_seconds,
            "limit": limit,
        },
        "ranked_opportunities": limited,
    }
    if export:
        result["files"] = export_opportunity_rankings(result)
    return result


def export_opportunity_rankings(report: dict[str, Any], report_dir: str | Path = "data/reports") -> dict[str, str]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "opportunity_rankings.json"
    csv_path = target / "opportunity_rankings.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    fields = [
        "rank",
        "id",
        "venue_pair",
        "market_type",
        "asset",
        "sport",
        "expected_value",
        "estimated_roi",
        "estimated_profit",
        "implied_probability_difference",
        "liquidity_score",
        "execution_score",
        "confidence_score",
        "freshness_score",
        "hedge_availability",
        "capital_efficiency_score",
        "ranking_score",
        "risk_notes",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in report.get("ranked_opportunities", []):
            writer.writerow({key: row.get(key) for key in fields})
    return {"json": str(json_path), "csv": str(csv_path)}


def _load_all_candidates(db_paths: tuple[str, ...], scan_globs: tuple[str, ...]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path in db_paths:
        candidates.extend(_load_sqlite_candidates(Path(path)))
    for pattern in scan_globs:
        for path in Path().glob(pattern):
            if path.name in {"opportunity_rankings.json"}:
                continue
            candidates.extend(_load_report_candidates(path))
    return candidates


def _load_sqlite_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "opportunities" not in tables:
            return []
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
        if "candidate_json" in columns:
            for row in conn.execute("SELECT * FROM opportunities").fetchall():
                item = dict(row)
                candidate = _loads(item.get("candidate_json")) or {}
                candidate.update({key: value for key, value in item.items() if key != "candidate_json" and key not in candidate})
                candidate["_source"] = str(path)
                rows.append(candidate)
        elif "raw_json" in columns:
            for row in conn.execute("SELECT * FROM opportunities").fetchall():
                item = dict(row)
                candidate = _loads(item.get("raw_json")) or {}
                candidate.update({key: value for key, value in item.items() if key != "raw_json" and key not in candidate})
                candidate["_source"] = str(path)
                rows.append(candidate)
    return rows


def _load_report_candidates(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("ranked_opportunities", "top_candidates", "top_scored_candidates", "opportunities", "price_aware_arbitrage_candidates", "arbitrage_candidates"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    rows.append({**item, "_source": str(path)})
    if isinstance(payload, dict):
        for container in ("scan", "result"):
            nested = payload.get(container)
            if isinstance(nested, dict):
                for item in _load_report_candidates_from_payload(nested, str(path)):
                    rows.append(item)
    return rows


def _load_report_candidates_from_payload(payload: dict[str, Any], source: str) -> list[dict[str, Any]]:
    rows = []
    for key in ("top_candidates", "top_scored_candidates", "opportunities", "arbitrage_candidates"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend({**item, "_source": source} for item in value if isinstance(item, dict))
    return rows


def _normalize_candidate(candidate: dict[str, Any], now: datetime) -> dict[str, Any]:
    timestamp = _parse_time(candidate.get("timestamp") or candidate.get("created_at") or candidate.get("scan_timestamp") or candidate.get("close_time"))
    age_seconds = max(0.0, (now - timestamp).total_seconds()) if timestamp else None
    estimated_roi = _first_float(candidate, "estimated_roi", "roi", "guaranteed_roi", "min_roi")
    edge = _first_float(candidate, "estimated_edge", "raw_edge", "edge", "guaranteed_roi") or 0.0
    estimated_profit = _first_float(candidate, "estimated_profit", "guaranteed_profit_amount", "profit", "min_profit") or 0.0
    expected_value = _first_float(candidate, "expected_value", "ev") 
    if expected_value is None:
        expected_value = estimated_profit if estimated_profit else edge
    implied_diff = _first_float(candidate, "implied_probability_difference", "probability_difference", "raw_edge", "estimated_edge") or edge
    liquidity_score = _score(_first_float(candidate, "liquidity_score", "liquidity"), scale=100.0)
    execution_score = _score(_first_float(candidate, "execution_score", "score", "ranking_score"))
    freshness_score = _freshness(age_seconds)
    confidence_score = _score(_first_float(candidate, "confidence_score", "match_confidence", "similarity_score"))
    hedge_availability = _hedge_score(candidate)
    capital_efficiency = _capital_efficiency(estimated_roi, estimated_profit)
    spread_penalty = _spread_penalty(candidate)
    weak_match_penalty = 1.0 - confidence_score
    one_sided_penalty = _one_sided_penalty(candidate)
    missing_hedge_penalty = 1.0 - hedge_availability
    ranking_score = (
        max(expected_value, 0.0) * 2.0
        + max(estimated_roi or 0.0, 0.0) * 20.0
        + max(implied_diff, 0.0) * 8.0
        + liquidity_score * 1.2
        + execution_score * 1.5
        + confidence_score * 1.5
        + freshness_score
        + hedge_availability
        + capital_efficiency
        - spread_penalty * 1.5
        - weak_match_penalty
        - missing_hedge_penalty * 0.8
        - one_sided_penalty * 1.2
    )
    risk_notes = _risk_notes(candidate, freshness_score, liquidity_score, spread_penalty, confidence_score, hedge_availability, one_sided_penalty)
    return {
        "id": candidate.get("id") or candidate.get("opportunity_id") or candidate.get("opportunity_key"),
        "source": candidate.get("_source"),
        "title": candidate.get("title") or candidate.get("market_title") or candidate.get("polymarket_title") or candidate.get("kalshi_title") or candidate.get("player"),
        "expected_value": round(expected_value, 6),
        "estimated_roi": round(float(estimated_roi or 0.0), 6),
        "estimated_profit": round(float(estimated_profit or 0.0), 6),
        "implied_probability_difference": round(float(implied_diff or 0.0), 6),
        "liquidity_score": round(liquidity_score, 4),
        "execution_score": round(execution_score, 4),
        "confidence_score": round(confidence_score, 4),
        "freshness_score": round(freshness_score, 4),
        "hedge_availability": round(hedge_availability, 4),
        "capital_efficiency_score": round(capital_efficiency, 4),
        "market_type": _market_type(candidate),
        "venue_pair": _venue_pair(candidate),
        "asset": _asset(candidate),
        "sport": _sport(candidate),
        "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
        "ranking_score": round(ranking_score, 6),
        "risk_notes": "; ".join(risk_notes) if risk_notes else "none",
        "raw": _safe_raw(candidate),
    }


def _passes_filters(row: dict[str, Any], *, venue: str | None, market_type: str | None, asset: str | None, sport: str | None, min_roi: float | None, min_confidence: float | None, max_age_seconds: int | None) -> bool:
    if venue and venue.lower() not in str(row.get("venue_pair") or "").lower():
        return False
    if market_type and row.get("market_type") != market_type.lower():
        return False
    if asset and row.get("asset") != asset.upper():
        return False
    if sport and row.get("sport") != sport.upper():
        return False
    if min_roi is not None and row.get("estimated_roi", 0) < min_roi:
        return False
    if min_confidence is not None and row.get("confidence_score", 0) < min_confidence:
        return False
    if max_age_seconds is not None and row.get("age_seconds") is not None and row["age_seconds"] > max_age_seconds:
        return False
    return True


def _risk_notes(candidate: dict[str, Any], freshness: float, liquidity: float, spread_penalty: float, confidence: float, hedge: float, one_sided: float) -> list[str]:
    notes = []
    if freshness < 0.4:
        notes.append("stale data")
    if liquidity < 0.3:
        notes.append("low liquidity")
    if spread_penalty > 0.4:
        notes.append("wide spread")
    if confidence < 0.5:
        notes.append("weak market matching")
    if hedge < 0.5:
        notes.append("missing hedge")
    if one_sided > 0:
        notes.append("one-sided directional exposure")
    return notes


def _market_type(candidate: dict[str, Any]) -> str:
    text = " ".join(str(candidate.get(key) or "") for key in ("market_type", "prop_type", "title", "market_title", "polymarket_title", "kalshi_title", "sport")).lower()
    if "prop" in text or "player" in text:
        return "prop"
    if any(token in text for token in ("future", "championship", "winner")):
        return "futures"
    if any(token in text for token in ("btc", "eth", "sol", "crypto", "bitcoin", "ethereum")):
        return "crypto"
    if any(token in text for token in ("nba", "nfl", "mlb", "nhl", "basketball", "baseball", "football", "hockey")):
        return "sports"
    return "event"


def _venue_pair(candidate: dict[str, Any]) -> str:
    pair = candidate.get("venue_pair")
    if pair:
        return str(pair)
    venues = []
    for venue, keys in {
        "kalshi": ("kalshi_ticker", "kalshi_title"),
        "polymarket": ("polymarket_id", "polymarket_title"),
        "sportsbook": ("sportsbook_event_id", "sportsbook", "bookmaker", "over_book", "under_book"),
    }.items():
        if any(candidate.get(key) for key in keys):
            venues.append(venue)
    return "_".join(venues) if venues else "unknown"


def _asset(candidate: dict[str, Any]) -> str | None:
    text = " ".join(str(candidate.get(key) or "") for key in ("asset", "ticker", "kalshi_ticker", "title", "market_title", "polymarket_title", "kalshi_title")).upper()
    for asset in ("BTC", "ETH", "SOL"):
        if asset in text or {"BTC": "BITCOIN", "ETH": "ETHEREUM", "SOL": "SOLANA"}[asset] in text:
            return asset
    return None


def _sport(candidate: dict[str, Any]) -> str | None:
    text = " ".join(str(candidate.get(key) or "") for key in ("sport", "league", "sport_key", "title", "market_title")).upper()
    mapping = {"MLB": ("MLB", "BASEBALL"), "NBA": ("NBA", "BASKETBALL"), "NFL": ("NFL", "FOOTBALL"), "NHL": ("NHL", "HOCKEY")}
    for sport, tokens in mapping.items():
        if any(token in text for token in tokens):
            return sport
    return None


def _hedge_score(candidate: dict[str, Any]) -> float:
    if candidate.get("hedge_available") is True or candidate.get("hedge_availability") is True:
        return 1.0
    if candidate.get("include_hedges") or candidate.get("hedge_candidates") or candidate.get("hedges"):
        return 0.8
    pair = _venue_pair(candidate)
    return 0.7 if "_" in pair or "/" in pair else 0.0


def _one_sided_penalty(candidate: dict[str, Any]) -> float:
    text = " ".join(str(candidate.get(key) or "") for key in ("side", "direction", "action", "strategy", "risk_notes")).lower()
    pair = _venue_pair(candidate)
    if ("buy" in text or "yes" in text or "directional" in text) and ("_" not in pair and "/" not in pair):
        return 1.0
    return 0.0


def _spread_penalty(candidate: dict[str, Any]) -> float:
    spread = _first_float(candidate, "spread", "bid_ask_spread", "kalshi_spread")
    if spread is None:
        return 0.2
    if spread > 1:
        spread /= 100.0
    return max(0.0, min(1.0, spread / 0.2))


def _capital_efficiency(roi: float | None, profit: float) -> float:
    roi_value = max(float(roi or 0.0), 0.0)
    profit_score = min(max(float(profit or 0.0), 0.0) / 50.0, 1.0)
    return round(min(1.0, roi_value * 10.0) * 0.7 + profit_score * 0.3, 4)


def _freshness(age_seconds: float | None) -> float:
    if age_seconds is None:
        return 0.5
    if age_seconds <= 0:
        return 1.0
    return round(max(0.0, min(1.0, math.exp(-age_seconds / 3600.0))), 4)


def _score(value: float | None, scale: float = 1.0) -> float:
    if value is None:
        return 0.5
    numeric = float(value)
    if scale != 1.0:
        numeric /= scale
    return max(0.0, min(1.0, numeric))


def _first_float(candidate: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = candidate.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _loads(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_raw(candidate: dict[str, Any]) -> dict[str, Any]:
    blocked = {"api_key", "token", "private_key", "secret", "authorization"}
    return {key: value for key, value in candidate.items() if not any(item in str(key).lower() for item in blocked) and not key.startswith("_")}


def _best_category(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    scores: dict[str, list[float]] = {}
    for row in rows:
        key = f"{row.get('venue_pair')}:{row.get('market_type')}"
        scores.setdefault(key, []).append(row["ranking_score"])
    return max(scores.items(), key=lambda item: mean(item[1]))[0]


def _data_warnings(raw: list[dict[str, Any]], normalized: list[dict[str, Any]]) -> list[str]:
    warnings = []
    if not raw:
        warnings.append("no local opportunities found in SQLite tables or scan outputs")
    if normalized and all(row["freshness_score"] < 0.4 for row in normalized):
        warnings.append("all opportunities appear stale")
    if normalized and all(row["liquidity_score"] < 0.3 for row in normalized):
        warnings.append("all opportunities have low liquidity")
    return warnings


def _decision_ready(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    best = rows[0]
    return bool(best["expected_value"] > 0 and best["estimated_roi"] > 0 and best["confidence_score"] >= 0.7 and best["execution_score"] >= 0.7 and best["freshness_score"] >= 0.5 and "missing hedge" not in best["risk_notes"])
