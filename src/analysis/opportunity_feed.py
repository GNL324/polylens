from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.analysis.opportunity_ranker import rank_opportunities
from src.sqlite_utils import closing_connection

DEFAULT_OPPORTUNITY_DBS = ("data/opportunities.db", "data/polylens.db")
DEFAULT_REPORT_GLOBS = ("data/reports/*.json",)
DEFAULT_PAPER_COPY_DB = "data/paper_copy_trader.db"
DEFAULT_SHORT_CRYPTO_PAPER_DB = "data/short_crypto_paper.db"


@dataclass
class FeedOpportunity:
    opportunity_id: str
    source: str
    strategy: str
    market_id: str
    title: str
    asset: str
    side: str
    entry_price: float
    target_price: float | None
    estimated_roi: float
    ranking_score: float
    eligible_for_paper_trading: bool
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_live_opportunities(
    *,
    db_paths: tuple[str, ...] = DEFAULT_OPPORTUNITY_DBS,
    report_globs: tuple[str, ...] = DEFAULT_REPORT_GLOBS,
    include_ranker: bool = True,
    include_auxiliary: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if include_ranker:
        rows.extend(_ranker_rows())
    for db_path in db_paths:
        rows.extend(_db_rows(Path(db_path)))
    if include_auxiliary:
        rows.extend(_paper_copy_rows())
        rows.extend(_short_crypto_paper_rows())
    rows.extend(_report_rows(report_globs))
    opportunities = _dedupe(_normalize(row) for row in rows)
    opportunities.sort(key=lambda item: (-item.ranking_score, item.source, item.opportunity_id))
    return [item.to_dict() for item in opportunities]


def get_paper_trading_opportunities(
    *,
    db_paths: tuple[str, ...] = DEFAULT_OPPORTUNITY_DBS,
    report_globs: tuple[str, ...] = DEFAULT_REPORT_GLOBS,
    limit: int | None = None,
    include_auxiliary: bool = True,
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in get_live_opportunities(db_paths=db_paths, report_globs=report_globs, include_auxiliary=include_auxiliary)
        if row["eligible_for_paper_trading"]
    ]
    if limit is not None:
        rows = rows[: max(int(limit), 0)]
    return rows


def opportunity_feed_status(
    *,
    db_paths: tuple[str, ...] = DEFAULT_OPPORTUNITY_DBS,
    report_globs: tuple[str, ...] = DEFAULT_REPORT_GLOBS,
    include_auxiliary: bool = True,
) -> dict[str, Any]:
    inventory = opportunity_inventory(db_paths=db_paths, report_globs=report_globs, include_auxiliary=include_auxiliary)
    opportunities = get_live_opportunities(db_paths=db_paths, report_globs=report_globs, include_auxiliary=include_auxiliary)
    sources: dict[str, int] = {}
    for item in opportunities:
        sources[item["source"]] = sources.get(item["source"], 0) + 1
    eligible = [item for item in opportunities if item["eligible_for_paper_trading"]]
    return {
        "sources": dict(sorted(sources.items())),
        "inventory": inventory,
        "total_opportunities": len(opportunities),
        "eligible_for_paper_trading": len(eligible),
    }


def opportunity_inventory(
    *,
    db_paths: tuple[str, ...] = DEFAULT_OPPORTUNITY_DBS,
    report_globs: tuple[str, ...] = DEFAULT_REPORT_GLOBS,
    include_auxiliary: bool = True,
) -> list[dict[str, Any]]:
    rows = [
        _inventory_row("opportunity_ranker", "python -m src.cli opportunity-ranker --json", len(_ranker_rows())),
        _inventory_row("short_crypto", "python -m src.cli scan-short-crypto --json", _table_count("data/polylens.db", "crypto_signals")),
        _inventory_row("short_crypto_paper", "python -m src.cli short-crypto-paper-report --json", _table_count(DEFAULT_SHORT_CRYPTO_PAPER_DB, "paper_trades")),
        _inventory_row("strategy_recommendations", "python -m src.cli strategy-recommendations --json", _report_count(report_globs, ("strategy_recommendations", "recommendations"))),
        _inventory_row("live_arbitrage", "python -m src.cli scan-live-arb --json", _alert_count(db_paths, "live_arbitrage")),
        _inventory_row("prop_arbitrage", "python -m src.cli scan-prop-arb --json", sum(_table_count(path, "prop_arbitrage_opportunities") for path in db_paths)),
        _inventory_row("opportunity_database", "sqlite data/* opportunities", sum(_table_count(path, "opportunities") for path in db_paths)),
        _inventory_row("recent_opportunities", "python -m src.cli recent-opportunities --json", sum(_table_count(path, "opportunities") for path in db_paths)),
        _inventory_row("recent_alerts", "python -m src.cli recent-alerts --json", sum(_table_count(path, "alert_events") + _table_count(path, "alerts") for path in db_paths)),
        _inventory_row("paper_copy_trader", "python -m src.cli paper-copy-trader --report --json", _table_count(DEFAULT_PAPER_COPY_DB, "paper_copy_positions")),
        _inventory_row("report_files", "data/reports/*.json", len(_report_rows(report_globs))),
    ]
    normalized = get_live_opportunities(db_paths=db_paths, report_globs=report_globs, include_ranker=False, include_auxiliary=include_auxiliary)
    eligible_by_source: dict[str, int] = {}
    for item in normalized:
        if item["eligible_for_paper_trading"]:
            eligible_by_source[item["source"]] = eligible_by_source.get(item["source"], 0) + 1
    for row in rows:
        row["eligible_for_paper_trading"] = eligible_by_source.get(row["source"], 0)
    return rows


def _inventory_row(source: str, command: str, count: int) -> dict[str, Any]:
    return {"source": source, "command": command, "records_available": int(count or 0), "eligible_for_paper_trading": 0}


def _ranker_rows() -> list[dict[str, Any]]:
    try:
        return [{**row, "_feed_source": "opportunity_ranker"} for row in rank_opportunities(limit=100).get("ranked_opportunities", []) if isinstance(row, dict)]
    except Exception:
        return []


def _db_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with closing_connection(path) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "opportunities" in tables:
            for row in conn.execute("SELECT * FROM opportunities").fetchall():
                rows.append(_row_payload(row, "opportunity_database", path))
        if "prop_arbitrage_opportunities" in tables:
            for row in conn.execute("SELECT * FROM prop_arbitrage_opportunities").fetchall():
                rows.append(_row_payload(row, "prop_arbitrage", path))
        if "alert_events" in tables:
            for row in conn.execute("SELECT * FROM alert_events").fetchall():
                payload = _loads(row["raw_json"]) or {}
                candidate = payload.get("candidate") if isinstance(payload, dict) else None
                base = candidate if isinstance(candidate, dict) else payload if isinstance(payload, dict) else {}
                rows.append({**base, **{key: row[key] for key in row.keys() if key != "raw_json"}, "_feed_source": _alert_source(row), "_source_path": str(path)})
        if "crypto_signals" in tables:
            for row in conn.execute("SELECT * FROM crypto_signals").fetchall():
                rows.append(_row_payload(row, "short_crypto", path))
    return rows


def _row_payload(row: Any, source: str, path: Path) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    raw = _loads(data.get("raw_json")) or _loads(data.get("candidate_json")) or {}
    if isinstance(raw, dict):
        data = {**raw, **data}
    data["_feed_source"] = source
    data["_source_path"] = str(path)
    return data


def _paper_copy_rows() -> list[dict[str, Any]]:
    path = Path(DEFAULT_PAPER_COPY_DB)
    if not path.exists():
        return []
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "paper_copy_positions" not in tables:
                return []
            return [{**{key: row[key] for key in row.keys()}, "_feed_source": "paper_copy_trader", "_source_path": str(path)} for row in conn.execute("SELECT * FROM paper_copy_positions").fetchall()]
    except Exception:
        return []


def _short_crypto_paper_rows() -> list[dict[str, Any]]:
    path = Path(DEFAULT_SHORT_CRYPTO_PAPER_DB)
    if not path.exists():
        return []
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "paper_signals" not in tables:
                return []
            return [{**{key: row[key] for key in row.keys()}, "_feed_source": "short_crypto_paper", "_source_path": str(path)} for row in conn.execute("SELECT * FROM paper_signals").fetchall()]
    except Exception:
        return []


def _report_rows(report_globs: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pattern in report_globs:
        for path in Path().glob(pattern):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows.extend(_extract_report_rows(payload, str(path)))
    return rows


def _extract_report_rows(payload: Any, source_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return rows
    for key in (
        "ranked_opportunities",
        "top_candidates",
        "top_scored_candidates",
        "prop_arbitrage_candidates",
        "arbitrage_candidates",
        "opportunities",
        "recommendations",
        "strategy_recommendations",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend({**item, "_feed_source": "report_files", "_source_path": source_path} for item in value if isinstance(item, dict))
    for key in ("scan", "result"):
        if isinstance(payload.get(key), dict):
            rows.extend(_extract_report_rows(payload[key], source_path))
    return rows


def _normalize(row: dict[str, Any]) -> FeedOpportunity:
    source = str(row.get("_feed_source") or row.get("source") or "unknown")
    opportunity_id = str(row.get("id") or row.get("opportunity_id") or row.get("opportunity_key") or row.get("polymarket_id") or row.get("kalshi_ticker") or _stable_key(row))
    title = str(row.get("title") or row.get("market_title") or row.get("polymarket_title") or row.get("player") or row.get("market") or "unknown")
    strategy = _strategy(row, source)
    estimated_roi = _first_float(row, "estimated_roi", "guaranteed_roi", "roi", "raw_edge", "estimated_edge") or 0.0
    ranking_score = _first_float(row, "ranking_score", "execution_score", "score", "confidence_score") or estimated_roi
    entry_price = _entry_price(row)
    target_price = _first_float(row, "target_price", "settlement_price", "exit_price")
    if target_price is None and entry_price > 0 and estimated_roi:
        target_price = min(0.99, max(0.01, entry_price * (1 + estimated_roi)))
    market_id = str(row.get("market_id") or row.get("polymarket_id") or row.get("kalshi_ticker") or row.get("ticker") or row.get("sportsbook_event_id") or opportunity_id)
    asset = _asset(row, title)
    side = str(row.get("side") or row.get("direction") or row.get("outcome") or "yes").lower()
    eligible = bool(market_id and title != "unknown" and 0 < entry_price < 1)
    return FeedOpportunity(
        opportunity_id=opportunity_id,
        source=source,
        strategy=strategy,
        market_id=market_id,
        title=title,
        asset=asset,
        side=side,
        entry_price=round(entry_price, 6),
        target_price=None if target_price is None else round(float(target_price), 6),
        estimated_roi=round(float(estimated_roi), 6),
        ranking_score=round(float(ranking_score), 6),
        eligible_for_paper_trading=eligible,
        raw=row,
    )


def _dedupe(items: Any) -> list[FeedOpportunity]:
    by_key: dict[str, FeedOpportunity] = {}
    for item in items:
        key = "|".join([item.market_id, item.side, item.strategy, item.title])
        existing = by_key.get(key)
        if existing is None or item.ranking_score > existing.ranking_score:
            by_key[key] = item
    return list(by_key.values())


def _strategy(row: dict[str, Any], source: str) -> str:
    if source in {"prop_arbitrage", "live_arbitrage", "short_crypto", "short_crypto_paper", "paper_copy_trader"}:
        return source
    return str(row.get("strategy") or row.get("opportunity_type") or row.get("market_type") or source)


def _entry_price(row: dict[str, Any]) -> float:
    value = _first_float(row, "entry_price", "price", "yes_ask", "best_price", "polymarket_implied_yes_price")
    if value is None:
        probability = _first_float(row, "implied_probability", "sportsbook_implied_probability")
        value = probability if probability is not None and 0 < probability < 1 else 0.5
    return min(0.99, max(0.01, float(value)))


def _asset(row: dict[str, Any], title: str) -> str:
    raw = str(row.get("asset") or row.get("sport") or "").upper()
    text = f"{raw} {title}".upper()
    if "BTC" in text or "BITCOIN" in text:
        return "BTC"
    if "ETH" in text or "ETHEREUM" in text:
        return "ETH"
    if "SOL" in text or "SOLANA" in text:
        return "SOL"
    return raw if raw else "OTHER"


def _alert_source(row: Any) -> str:
    alert_type = str(row["alert_type"] or "")
    if "prop" in alert_type:
        return "prop_arbitrage"
    if "crypto" in alert_type:
        return "short_crypto"
    return "live_arbitrage"


def _table_count(db_path: str | Path, table: str) -> int:
    path = Path(db_path)
    if not path.exists():
        return 0
    try:
        with closing_connection(path) as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in tables:
                return 0
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return 0


def _alert_count(db_paths: tuple[str, ...], alert_type: str) -> int:
    count = 0
    for path in db_paths:
        if not Path(path).exists():
            continue
        try:
            with closing_connection(path) as conn:
                tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "alert_events" in tables:
                    count += int(conn.execute("SELECT COUNT(*) FROM alert_events WHERE alert_type=?", (alert_type,)).fetchone()[0])
        except Exception:
            pass
    return count


def _report_count(report_globs: tuple[str, ...], keys: tuple[str, ...]) -> int:
    return sum(1 for row in _report_rows(report_globs) if any(key in row for key in keys))


def _loads(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


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


def _stable_key(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
