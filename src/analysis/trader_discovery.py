from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.analysis.trader_registry import DEFAULT_TRADERS_DB, list_traders
from src.analysis.trader_scanner import DEFAULT_WATCHLIST_PATH, scan_wallets
from src.analysis.wallet_activity import export_wallet_activity, validate_wallet, write_wallet_activity_export
from src.sqlite_utils import closing_connection

LOGGER = logging.getLogger(__name__)

DEFAULT_TRADER_DISCOVERY_DB = "data/trader_discovery.db"
DEFAULT_WALLET_EXPORT_DIR = "data/wallets"
POLYMARKET_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
POLYMARKET_TRADES_URL = "https://data-api.polymarket.com/trades"
WALLET_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


@dataclass
class TraderDiscoveryCandidate:
    wallet: str
    source: str
    discovery_score: int
    evidence_count: int
    markets_seen: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_from_wallet(
    wallet: str,
    limit: int | None = None,
    output_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
) -> list[str]:
    wallet = _normalize_wallet(wallet)
    candidates = discover_candidates_from_wallet(wallet, limit=limit, output_dir=output_dir, db_path=db_path)
    return [candidate.wallet for candidate in candidates]


def discover_from_registry(
    db_path: str | Path = DEFAULT_TRADERS_DB,
    limit: int | None = None,
) -> list[str]:
    rows = list_traders(limit=limit or 10_000, db_path=str(db_path))
    return _dedupe_wallets((row.wallet for row in rows), limit=limit)


def discover_from_activity_export(
    path: str | Path,
    db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int | None = None,
) -> list[str]:
    candidates = discover_candidates_from_activity_export(path, db_path=db_path, limit=limit)
    return [candidate.wallet for candidate in candidates]


def discover_traders(
    wallet: str | None = None,
    activity_export: str | Path | None = None,
    watchlist: str | Path = DEFAULT_WATCHLIST_PATH,
    registry_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int | None = None,
) -> list[str]:
    candidates = discover_trader_candidates(
        wallet=wallet,
        activity_export=activity_export,
        watchlist=watchlist,
        registry_db_path=registry_db_path,
        discovery_db_path=discovery_db_path,
        limit=limit,
    )
    return [candidate.wallet for candidate in candidates]


def discover_trader_candidates(
    wallet: str | None = None,
    activity_export: str | Path | None = None,
    watchlist: str | Path = DEFAULT_WATCHLIST_PATH,
    registry_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int | None = None,
) -> list[TraderDiscoveryCandidate]:
    candidates: list[TraderDiscoveryCandidate] = []
    if wallet:
        candidates.extend(discover_candidates_from_wallet(wallet, limit=limit, db_path=discovery_db_path))
    if activity_export:
        candidates.extend(discover_candidates_from_activity_export(activity_export, db_path=discovery_db_path))
    if not wallet and not activity_export:
        candidates.extend(_candidates_from_watchlist(watchlist))
        candidates.extend(_candidates_from_registry(registry_db_path))

    merged = _merge_candidates(candidates)
    for candidate in merged:
        save_discovery_candidate(candidate, db_path=discovery_db_path)
    return _limit_candidates(merged, limit)


def discover_candidates_from_activity_export(
    path: str | Path,
    db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int | None = None,
) -> list[TraderDiscoveryCandidate]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    source_wallet = str(payload.get("wallet") or "").lower() if isinstance(payload, dict) else ""
    candidates = score_counterparty_candidates(payload, source_wallet=source_wallet, source="counterparty")
    for candidate in candidates:
        save_discovery_candidate(candidate, db_path=db_path)
    return _limit_candidates(candidates, limit)


def discover_candidates_from_wallet(
    wallet: str,
    limit: int | None = None,
    output_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    market_activity_limit: int = 100,
) -> list[TraderDiscoveryCandidate]:
    wallet = _normalize_wallet(wallet)
    output_path = Path(output_dir) / f"{wallet}_activity.json"
    if output_path.exists():
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        export = export_wallet_activity(wallet, limit=limit)
        write_wallet_activity_export(export, output_path)
        payload = export.to_dict()

    candidates = score_counterparty_candidates(payload, source_wallet=wallet, source="counterparty")
    candidates.extend(discover_market_participants(payload, source_wallet=wallet, limit=market_activity_limit))
    merged = _merge_candidates(candidates)
    for candidate in merged:
        save_discovery_candidate(candidate, db_path=db_path)
    return _limit_candidates(merged, limit)


def discover_market_participants(
    payload: Any,
    source_wallet: str,
    limit: int = 100,
    max_markets: int = 25,
) -> list[TraderDiscoveryCandidate]:
    source_wallet = source_wallet.lower()
    events = _extract_events(payload)
    market_refs = _market_refs_from_events(events)[:max_markets]
    evidence: dict[str, int] = defaultdict(int)
    markets: dict[str, set[str]] = defaultdict(set)

    for ref in market_refs:
        for row in _fetch_market_activity(ref, limit=limit):
            if not isinstance(row, dict):
                continue
            if not _row_matches_market_ref(row, ref):
                continue
            market = _market_key(row) or ref.get("market") or ref.get("slug") or ""
            for wallet in _extract_wallets(row):
                if wallet == source_wallet:
                    continue
                evidence[wallet] += 1
                if market:
                    markets[wallet].add(market)

    return _limit_candidates(
        [
            TraderDiscoveryCandidate(
                wallet=wallet,
                source="co_market_activity",
                discovery_score=_score_candidate(count, markets[wallet]),
                evidence_count=count,
                markets_seen=sorted(markets[wallet]),
            )
            for wallet, count in evidence.items()
        ],
        None,
    )


def score_counterparty_candidates(payload: Any, source_wallet: str = "", source: str = "counterparty") -> list[TraderDiscoveryCandidate]:
    events = _extract_events(payload)
    source_wallet = source_wallet.lower()
    evidence: dict[str, int] = defaultdict(int)
    markets: dict[str, set[str]] = defaultdict(set)

    for event in events:
        if not isinstance(event, dict):
            continue
        market = _market_key(event)
        for wallet in _extract_wallets(event):
            if wallet == source_wallet:
                continue
            evidence[wallet] += 1
            if market:
                markets[wallet].add(market)

    candidates = [
        TraderDiscoveryCandidate(
            wallet=wallet,
            source=source,
            discovery_score=_score_candidate(count, markets[wallet]),
            evidence_count=count,
            markets_seen=sorted(markets[wallet]),
        )
        for wallet, count in evidence.items()
    ]
    return _limit_candidates(candidates, None)


def save_discovery_candidate(candidate: TraderDiscoveryCandidate, db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB) -> None:
    _init_discovery_db(db_path)
    now = _now_iso()
    with closing_connection(Path(db_path)) as conn:
        existing = conn.execute("SELECT first_seen, discovery_score, evidence_count FROM discovered_wallets WHERE wallet = ?", (candidate.wallet,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE discovered_wallets
                SET last_seen = ?, discovery_score = ?, discovery_source = ?, evidence_count = ?
                WHERE wallet = ?
                """,
                (
                    now,
                    max(int(existing["discovery_score"]), candidate.discovery_score),
                    candidate.source,
                    max(int(existing["evidence_count"]), candidate.evidence_count),
                    candidate.wallet,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO discovered_wallets
                (wallet, first_seen, last_seen, discovery_score, discovery_source, evidence_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (candidate.wallet, now, now, candidate.discovery_score, candidate.source, candidate.evidence_count),
            )


def load_discovered_wallets(db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB, limit: int | None = None) -> list[TraderDiscoveryCandidate]:
    _init_discovery_db(db_path)
    query = """
        SELECT wallet, discovery_source, discovery_score, evidence_count
        FROM discovered_wallets
        ORDER BY discovery_score DESC, evidence_count DESC, wallet ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (int(limit),)
    with closing_connection(Path(db_path)) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        TraderDiscoveryCandidate(
            wallet=row["wallet"],
            source=row["discovery_source"],
            discovery_score=int(row["discovery_score"]),
            evidence_count=int(row["evidence_count"]),
            markets_seen=[],
        )
        for row in rows
    ]


def discovery_report(
    wallet: str | None = None,
    activity_export: str | Path | None = None,
    watchlist: str | Path = DEFAULT_WATCHLIST_PATH,
    registry_db_path: str | Path = DEFAULT_TRADERS_DB,
    discovery_db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB,
    limit: int | None = None,
    scan: bool = False,
) -> dict[str, Any]:
    existing_before = {candidate.wallet for candidate in load_discovered_wallets(discovery_db_path)}
    candidates = discover_trader_candidates(
        wallet=wallet,
        activity_export=activity_export,
        watchlist=watchlist,
        registry_db_path=registry_db_path,
        discovery_db_path=discovery_db_path,
        limit=limit,
    )
    wallets = [candidate.wallet for candidate in candidates]
    new_wallets = [wallet for wallet in wallets if wallet not in existing_before]
    report: dict[str, Any] = {
        "wallets_discovered": len(wallets),
        "new_wallets": len(new_wallets),
        "existing_wallets": len(wallets) - len(new_wallets),
        "top_candidates": [
            {
                "wallet": candidate.wallet,
                "score": candidate.discovery_score,
                "source": candidate.source,
                "evidence_count": candidate.evidence_count,
                "markets_seen": candidate.markets_seen,
            }
            for candidate in candidates[:10]
        ],
    }
    if scan and wallets:
        report["scan"] = scan_wallets(wallets=wallets, limit=limit, include_registry=False)
    return report


def _init_discovery_db(db_path: str | Path = DEFAULT_TRADER_DISCOVERY_DB) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing_connection(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovered_wallets (
                wallet TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                discovery_score INTEGER NOT NULL,
                discovery_source TEXT NOT NULL,
                evidence_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovered_wallets_wallet ON discovered_wallets(wallet);
            CREATE INDEX IF NOT EXISTS idx_discovered_wallets_score ON discovered_wallets(discovery_score DESC);
            """
        )


def _candidates_from_registry(db_path: str | Path) -> list[TraderDiscoveryCandidate]:
    candidates = []
    for index, wallet in enumerate(discover_from_registry(db_path=db_path)):
        score = max(1, 30 - index)
        candidates.append(TraderDiscoveryCandidate(wallet=wallet, source="registry", discovery_score=score, evidence_count=1, markets_seen=[]))
    return candidates


def _candidates_from_watchlist(path: str | Path) -> list[TraderDiscoveryCandidate]:
    watchlist = Path(path)
    if not watchlist.exists():
        return []
    payload = json.loads(watchlist.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("watchlist JSON must be a list of wallet addresses")
    return [
        TraderDiscoveryCandidate(wallet=wallet, source="watchlist", discovery_score=35, evidence_count=1, markets_seen=[])
        for wallet in _dedupe_wallets(payload)
    ]


def _candidates_from_known_wallet_exports(wallet: str, db_path: str | Path) -> list[TraderDiscoveryCandidate]:
    wallet = _normalize_wallet(wallet)
    paths = [
        Path(DEFAULT_WALLET_EXPORT_DIR) / f"{wallet}_activity.json",
        Path(DEFAULT_WALLET_EXPORT_DIR) / f"{wallet}.json",
    ]
    candidates: list[TraderDiscoveryCandidate] = []
    for path in paths:
        if path.exists():
            candidates.extend(discover_candidates_from_activity_export(path, db_path=db_path))
    return candidates


def _market_refs_from_events(events: list[Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        ref = {
            "conditionId": str(event.get("condition_id") or event.get("conditionId") or "").strip(),
            "market": str(event.get("market_id") or event.get("market") or "").strip(),
            "slug": str(event.get("market_slug") or event.get("slug") or "").strip(),
        }
        raw = event.get("raw")
        if isinstance(raw, dict):
            ref["conditionId"] = ref["conditionId"] or str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
            ref["market"] = ref["market"] or str(raw.get("market") or raw.get("marketId") or raw.get("conditionId") or "").strip()
            ref["slug"] = ref["slug"] or str(raw.get("slug") or raw.get("eventSlug") or "").strip()
        key = (ref["conditionId"] or ref["market"], ref["slug"])
        if not any(ref.values()) or key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def _fetch_market_activity(ref: dict[str, str], limit: int = 100) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    if ref.get("conditionId"):
        queries.append({"market": ref["conditionId"], "limit": limit, "offset": 0})
    if ref.get("market") and ref.get("market") != ref.get("conditionId"):
        queries.append({"market": ref["market"], "limit": limit, "offset": 0})

    for query in queries:
        url = f"{POLYMARKET_TRADES_URL}?{urlencode(query)}"
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "polylens/0.1"})
            payload = json.loads(urlopen(request, timeout=20).read().decode("utf-8"))
        except Exception as exc:
            LOGGER.debug("market participant discovery failed query=%s error=%s", query, exc)
            continue
        if isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))
        if any(_row_matches_market_ref(row, ref) for row in rows):
            break
    return rows


def _row_matches_market_ref(row: dict[str, Any], ref: dict[str, str]) -> bool:
    row_values = {
        str(row.get("conditionId") or row.get("condition_id") or ""),
        str(row.get("market") or row.get("marketId") or row.get("market_id") or ""),
        str(row.get("slug") or row.get("eventSlug") or row.get("market_slug") or ""),
    }
    raw = row.get("raw")
    if isinstance(raw, dict):
        row_values.update(
            {
                str(raw.get("conditionId") or raw.get("condition_id") or ""),
                str(raw.get("market") or raw.get("marketId") or ""),
                str(raw.get("slug") or raw.get("eventSlug") or ""),
            }
        )
    ref_values = {value for value in (ref.get("conditionId"), ref.get("market"), ref.get("slug")) if value}
    return bool(ref_values & {value for value in row_values if value})


def _extract_events(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("events", "activities", "activity", "payload", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _extract_wallets(value: Any) -> set[str]:
    wallets: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _walletish_key(str(key)):
                wallets.update(_wallets_from_scalar(item))
            elif isinstance(item, (dict, list)):
                wallets.update(_extract_wallets(item))
    elif isinstance(value, list):
        for item in value:
            wallets.update(_extract_wallets(item))
    else:
        wallets.update(_wallets_from_scalar(value))
    return wallets


def _wallets_from_scalar(value: Any) -> set[str]:
    if value is None:
        return set()
    return {match.group(0).lower() for match in WALLET_RE.finditer(str(value))}


def _walletish_key(key: str) -> bool:
    key = key.lower()
    return any(part in key for part in ("wallet", "address", "maker", "taker", "trader", "user"))


def _market_key(event: dict[str, Any]) -> str:
    for key in ("condition_id", "conditionId", "market_id", "market", "market_slug", "slug", "market_title", "title"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    raw = event.get("raw")
    if isinstance(raw, dict):
        return _market_key(raw)
    return ""


def _score_candidate(evidence_count: int, markets_seen: set[str]) -> int:
    repeat_bonus = min(40, max(0, evidence_count - 1) * 10)
    market_bonus = min(30, len(markets_seen) * 8)
    return min(100, 20 + repeat_bonus + market_bonus)


def _merge_candidates(candidates: list[TraderDiscoveryCandidate]) -> list[TraderDiscoveryCandidate]:
    merged: dict[str, TraderDiscoveryCandidate] = {}
    for candidate in candidates:
        current = merged.get(candidate.wallet)
        if current is None:
            merged[candidate.wallet] = TraderDiscoveryCandidate(
                wallet=candidate.wallet,
                source=candidate.source,
                discovery_score=candidate.discovery_score,
                evidence_count=candidate.evidence_count,
                markets_seen=list(candidate.markets_seen),
            )
            continue
        previous_score = current.discovery_score
        current.evidence_count += candidate.evidence_count
        current.markets_seen = sorted(set(current.markets_seen) | set(candidate.markets_seen))
        current.discovery_score = max(current.discovery_score, _score_candidate(current.evidence_count, set(current.markets_seen)), candidate.discovery_score)
        if candidate.discovery_score > previous_score:
            current.source = candidate.source
    return _limit_candidates(list(merged.values()), None)


def _limit_candidates(candidates: list[TraderDiscoveryCandidate], limit: int | None) -> list[TraderDiscoveryCandidate]:
    ordered = sorted(candidates, key=lambda item: (-item.discovery_score, -item.evidence_count, item.wallet))
    if limit is None:
        return ordered
    return ordered[: max(0, int(limit))]


def _dedupe_wallets(values: Any, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    wallets: list[str] = []
    for value in values:
        wallet = _normalize_wallet(value)
        if wallet in seen:
            continue
        seen.add(wallet)
        wallets.append(wallet)
        if limit is not None and len(wallets) >= int(limit):
            break
    return wallets


def _normalize_wallet(wallet: Any) -> str:
    wallet = str(wallet or "").strip().lower()
    validate_wallet(wallet)
    return wallet


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
