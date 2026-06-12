from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.sqlite_utils import closing_connection

LOGGER = logging.getLogger(__name__)

DEFAULT_WALLET_ACTIVITY_DB = "data/wallet_activity.db"
POLYMARKET_ACTIVITY_URL = "https://data-api.polymarket.com/activity"
SUPPORTED_ACTIONS = {"buy", "sell", "redeem", "merge"}
FUTURE_ACTIONS = {"transfer", "liquidity_add", "liquidity_remove"}
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
POLYMARKET_MAX_HISTORICAL_ACTIVITY_OFFSET = 3000


class HistoricalActivityLimitReached(RuntimeError):
    pass


@dataclass
class WalletActivityEvent:
    wallet: str
    timestamp: float
    event_type: str
    market_id: str
    condition_id: str
    market_slug: str
    market_title: str
    asset: str
    side: str
    action: str
    shares: float
    price: float
    amount: float
    tx_hash: str
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalletActivityExport:
    wallet: str
    export_timestamp: str
    source: str
    event_count: int
    events: list[WalletActivityEvent]

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "export_timestamp": self.export_timestamp,
            "source": self.source,
            "event_count": self.event_count,
            "events": [event.to_dict() for event in self.events],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


class WalletActivitySource(Protocol):
    name: str

    def fetch_activity(self, wallet: str, limit: int | None = None) -> list[dict[str, Any]]:
        ...


class PolymarketActivitySource:
    name = "polymarket-data-api/activity"

    def __init__(
        self,
        base_url: str = POLYMARKET_ACTIVITY_URL,
        page_size: int = 500,
        timeout: int = 20,
        retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        max_historical_offset: int = POLYMARKET_MAX_HISTORICAL_ACTIVITY_OFFSET,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.page_size = max(1, min(int(page_size or 500), 500))
        self.timeout = timeout
        self.retries = max(0, retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.max_historical_offset = max_historical_offset
        self._sleep = sleep

    def fetch_activity(self, wallet: str, limit: int | None = None) -> list[dict[str, Any]]:
        validate_wallet(wallet)
        target = max(0, int(limit)) if limit is not None else None
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            remaining = None if target is None else target - len(rows)
            if remaining is not None and remaining <= 0:
                break
            if self.max_historical_offset is not None and offset > self.max_historical_offset:
                LOGGER.info("stopping Polymarket activity pagination at max historical offset=%s", self.max_historical_offset)
                break
            page_limit = self.page_size if remaining is None else min(self.page_size, remaining)
            try:
                payload = self._get_page(wallet=wallet, limit=page_limit, offset=offset)
            except HistoricalActivityLimitReached:
                LOGGER.info("Polymarket activity history exhausted at offset=%s wallet=%s", offset, wallet)
                break
            if not isinstance(payload, list):
                LOGGER.warning("Polymarket activity returned non-list payload for wallet=%s", wallet)
                break
            rows.extend(row for row in payload if isinstance(row, dict))
            if len(payload) < page_limit:
                break
            offset += page_limit
        return rows[:target] if target is not None else rows

    def _get_page(self, wallet: str, limit: int, offset: int) -> Any:
        params = urlencode({"user": wallet, "limit": limit, "offset": offset})
        url = f"{self.base_url}?{params}"
        attempt = 0
        while True:
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "polylens/0.1"})
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body else []
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 400 and _is_historical_activity_limit_error(detail):
                    raise HistoricalActivityLimitReached(detail) from exc
                retry_after = _retry_after_seconds(exc)
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise RuntimeError(f"Polymarket activity request failed HTTP {exc.code}: {detail[:200]}") from exc
                delay = retry_after if retry_after is not None else self.retry_backoff_seconds * (2**attempt)
                LOGGER.warning("Polymarket activity rate/transport error HTTP %s; retrying in %.2fs", exc.code, delay)
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= self.retries:
                    raise RuntimeError(f"Polymarket activity request failed: {exc}") from exc
                delay = self.retry_backoff_seconds * (2**attempt)
                LOGGER.warning("Polymarket activity request failed; retrying in %.2fs: %s", delay, exc)
            attempt += 1
            if delay > 0:
                self._sleep(delay)


def validate_wallet(wallet: str) -> None:
    if not WALLET_RE.match(str(wallet or "")):
        raise ValueError("wallet must be a 0x-prefixed 40 character hex address")


def export_wallet_activity(
    wallet: str,
    limit: int | None = None,
    source: WalletActivitySource | None = None,
    db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB,
    store: bool = True,
) -> WalletActivityExport:
    validate_wallet(wallet)
    source = source or PolymarketActivitySource()
    try:
        raw_rows = source.fetch_activity(wallet, limit=limit)
    except Exception as exc:
        LOGGER.warning("wallet activity export failed wallet=%s source=%s: %s", wallet, source.name, exc)
        raw_rows = []
    events = normalize_activity_payload(raw_rows, wallet)
    export = WalletActivityExport(
        wallet=wallet.lower(),
        export_timestamp=_utc_now(),
        source=source.name,
        event_count=len(events),
        events=events,
    )
    if store:
        save_wallet_activity_export(export, db_path=db_path)
    return export


def normalize_activity_payload(payload: Any, wallet: str) -> list[WalletActivityEvent]:
    rows = _extract_rows(payload)
    events: list[WalletActivityEvent] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = normalize_activity_event(row, wallet)
        if event is None:
            continue
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    events.sort(key=lambda event: (event.timestamp, event.tx_hash, event.market_id, event.condition_id, event.action, event.side))
    return events


def normalize_activity_event(record: dict[str, Any], wallet: str) -> WalletActivityEvent | None:
    action = normalize_action(record)
    if action not in SUPPORTED_ACTIONS and action not in FUTURE_ACTIONS:
        return None
    event_type = normalize_event_type(record)
    timestamp = _safe_float(_first(record, "timestamp", "time", "createdAt", "created_at"))
    market_title = str(_first(record, "title", "question", "marketTitle", "market_title", "slug") or "unknown")
    market_slug = str(_first(record, "slug", "marketSlug", "market_slug", "eventSlug") or "")
    condition_id = str(_first(record, "conditionId", "condition_id") or "")
    market_id = str(_first(record, "market", "marketId", "market_id", "conditionId", "condition_id", "slug") or "")
    side = normalize_side(_first(record, "outcome", "sideOutcome", "outcomeName", "assetName"), record)
    shares = _safe_float(_first(record, "shares", "size", "quantity", "amountShares"))
    price = _safe_float(_first(record, "price", "avgPrice", "averagePrice"))
    amount = _amount(record, shares=shares, price=price)
    return WalletActivityEvent(
        wallet=wallet.lower(),
        timestamp=timestamp,
        event_type=event_type,
        market_id=market_id,
        condition_id=condition_id,
        market_slug=market_slug,
        market_title=market_title,
        asset=infer_asset(market_title, market_slug),
        side=side,
        action=action,
        shares=shares,
        price=price,
        amount=amount,
        tx_hash=str(_first(record, "transactionHash", "txHash", "tx_hash", "hash") or ""),
        raw=dict(record),
    )


def normalize_event_type(record: dict[str, Any]) -> str:
    text = str(_first(record, "event_type", "type", "activityType", "activity_type") or "").strip()
    return _canonical_token(text) if text else normalize_action(record)


def normalize_action(record: dict[str, Any]) -> str:
    for key in ("action", "type", "event_type", "activityType", "activity_type"):
        token = _canonical_token(record.get(key))
        if token in {"buy", "sell", "redeem", "merge", "transfer"}:
            return token
        if token in {"trade_buy", "trade_buy_yes", "buy_yes"}:
            return "buy"
        if token in {"trade_sell", "trade_sell_yes", "sell_yes"}:
            return "sell"
        if token == "trade":
            side = _canonical_token(record.get("side"))
            if side in {"buy", "sell"}:
                return side
        if token in {"liquidity_add", "add_liquidity"}:
            return "liquidity_add"
        if token in {"liquidity_remove", "remove_liquidity"}:
            return "liquidity_remove"
    side = _canonical_token(record.get("side"))
    if side in {"buy", "sell"}:
        return side
    return ""


def normalize_side(value: Any, record: dict[str, Any] | None = None) -> str:
    token = _canonical_token(value)
    if token in {"up", "down", "yes", "no"}:
        return token
    if token in {"0", "1"} and record is not None:
        return _side_from_index(int(token), record)
    if record is not None:
        raw_index = _first(record, "outcomeIndex", "outcome_index", "outcome_id")
        try:
            return _side_from_index(int(raw_index), record)
        except (TypeError, ValueError):
            return ""
    return ""


def infer_asset(title: str, slug: str = "") -> str:
    text = f"{title} {slug}".upper()
    if "BTC" in text or "BITCOIN" in text:
        return "BTC"
    if "ETH" in text or "ETHEREUM" in text:
        return "ETH"
    if "SOL" in text or "SOLANA" in text:
        return "SOL"
    return "OTHER"


def save_wallet_activity_export(export: WalletActivityExport, db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB) -> int:
    _init_wallet_activity_db(db_path)
    with closing_connection(Path(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO wallet_exports (wallet, export_timestamp, source, event_count, export_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (export.wallet, export.export_timestamp, export.source, export.event_count, json.dumps(export.to_dict(), sort_keys=True)),
        )
        export_id = int(cur.lastrowid)
        for event in export.events:
            conn.execute(
                """
                INSERT OR IGNORE INTO wallet_events
                (export_id, wallet, timestamp, event_type, market_id, condition_id, market_slug, market_title, asset, side, action, shares, price, amount, tx_hash, event_key, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    event.wallet,
                    event.timestamp,
                    event.event_type,
                    event.market_id,
                    event.condition_id,
                    event.market_slug,
                    event.market_title,
                    event.asset,
                    event.side,
                    event.action,
                    event.shares,
                    event.price,
                    event.amount,
                    event.tx_hash,
                    _event_key(event),
                    json.dumps(event.raw, sort_keys=True),
                ),
            )
    return export_id


def _init_wallet_activity_db(db_path: str | Path = DEFAULT_WALLET_ACTIVITY_DB) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing_connection(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                export_timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                export_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_events (
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
            CREATE INDEX IF NOT EXISTS idx_wallet_exports_wallet_time ON wallet_exports(wallet, export_timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet_time ON wallet_events(wallet, timestamp);
            CREATE INDEX IF NOT EXISTS idx_wallet_events_action ON wallet_events(action);
            CREATE INDEX IF NOT EXISTS idx_wallet_events_market ON wallet_events(condition_id, market_id);
            """
        )


def write_wallet_activity_export(export: WalletActivityExport, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export.to_json() + "\n", encoding="utf-8")
    return path


def _extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("events", "activities", "activity", "data", "payload"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _event_key(event: WalletActivityEvent) -> str:
    parts = [
        event.wallet,
        event.tx_hash,
        str(event.timestamp),
        event.market_id,
        event.condition_id,
        event.action,
        event.side,
        str(event.shares),
        str(event.price),
        str(event.amount),
    ]
    return "|".join(parts)


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _amount(record: dict[str, Any], shares: float, price: float) -> float:
    value = _first(record, "amount", "usdcSize", "usdc_size", "notional", "value")
    if value not in (None, ""):
        return round(_safe_float(value), 6)
    return round(shares * price, 6)


def _canonical_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _side_from_index(index: int, record: dict[str, Any]) -> str:
    if index not in (0, 1):
        return ""
    title = str(_first(record, "title", "question", "slug") or "").lower()
    if "up or down" in title or "up/down" in title or "updown" in title:
        return "up" if index == 0 else "down"
    return "yes" if index == 0 else "no"


def _retry_after_seconds(exc: HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _is_historical_activity_limit_error(detail: str) -> bool:
    return "max historical activity offset" in str(detail or "").lower()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
