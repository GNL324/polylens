from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.analysis.trader_scanner import DEFAULT_WALLET_EXPORT_DIR
from src.analysis.wallet_activity import (
    WalletActivityEvent,
    export_wallet_activity,
    normalize_activity_payload,
    validate_wallet,
    write_wallet_activity_export,
)

SESSION_GAP_MINUTES = 60
TRADE_ACTIONS = frozenset({"buy", "sell"})
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DURATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b5\s*(?:min(?:ute)?s?|m)\b", re.I), "5m"),
    (re.compile(r"\b15\s*(?:min(?:ute)?s?|m)\b", re.I), "15m"),
    (re.compile(r"\b30\s*(?:min(?:ute)?s?|m)\b", re.I), "30m"),
    (re.compile(r"\b(?:1\s*h(?:our)?s?|hourly)\b", re.I), "hourly"),
    (re.compile(r"\b4\s*h(?:our)?s?\b", re.I), "4h"),
    (re.compile(r"\bdaily\b", re.I), "daily"),
)


@dataclass
class TraderReplayEvent:
    timestamp: str
    wallet: str
    action: str
    side: str
    asset: str
    market_id: str
    market_title: str
    shares: float
    amount: float
    price: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_timeline_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "action": self.action,
        }
        if self.asset:
            payload["asset"] = self.asset
        if self.side:
            payload["side"] = self.side
        if self.market_id:
            payload["market_id"] = self.market_id
        if self.market_title:
            payload["market_title"] = self.market_title
        if self.shares:
            payload["shares"] = self.shares
        if self.amount:
            payload["amount"] = self.amount
        if self.price:
            payload["price"] = self.price
        return payload


@dataclass
class TraderReplayReport:
    wallet: str
    event_count: int
    first_activity: str
    last_activity: str
    timeline: list[dict[str, Any]]
    statistics: dict[str, Any]
    patterns: dict[str, Any]

    def to_dict(self, *, include_timeline: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "wallet": self.wallet,
            "event_count": self.event_count,
            "first_activity": self.first_activity,
            "last_activity": self.last_activity,
            "statistics": self.statistics,
            "patterns": self.patterns,
        }
        if include_timeline:
            payload["timeline"] = self.timeline
        return payload


def build_trader_replay(
    wallet: str,
    *,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    export_if_missing: bool = True,
    activity_limit: int | None = None,
    recent_limit: int | None = None,
    include_timeline: bool = True,
) -> TraderReplayReport:
    wallet = str(wallet or "").strip().lower()
    validate_wallet(wallet)
    events = load_wallet_replay_events(
        wallet,
        wallet_export_dir=wallet_export_dir,
        export_if_missing=export_if_missing,
        activity_limit=activity_limit,
    )
    replay_events = [_replay_event_from_wallet_event(event) for event in events]
    statistics = _compute_statistics(events)
    patterns = _compute_patterns(events, statistics)
    timeline = _build_timeline(replay_events, recent_limit=recent_limit) if include_timeline else []
    first_activity = replay_events[0].timestamp if replay_events else ""
    last_activity = replay_events[-1].timestamp if replay_events else ""
    return TraderReplayReport(
        wallet=wallet,
        event_count=len(replay_events),
        first_activity=first_activity,
        last_activity=last_activity,
        timeline=timeline,
        statistics=statistics,
        patterns=patterns,
    )


def load_wallet_replay_events(
    wallet: str,
    *,
    wallet_export_dir: str | Path = DEFAULT_WALLET_EXPORT_DIR,
    export_if_missing: bool = True,
    activity_limit: int | None = None,
) -> list[WalletActivityEvent]:
    wallet = str(wallet or "").strip().lower()
    validate_wallet(wallet)
    export_path = Path(wallet_export_dir) / f"{wallet}_activity.json"
    if not export_path.exists():
        if not export_if_missing:
            raise FileNotFoundError(f"wallet activity export not found: {export_path}")
        export = export_wallet_activity(wallet, limit=activity_limit)
        write_wallet_activity_export(export, export_path)
        return list(export.events)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    rows = payload.get("events", payload) if isinstance(payload, dict) else payload
    return normalize_activity_payload(rows, wallet)


def infer_market_type(market_title: str, market_slug: str = "", asset: str = "") -> str:
    text = f"{market_title} {market_slug}".strip()
    duration = ""
    for pattern, label in _DURATION_PATTERNS:
        if pattern.search(text):
            duration = label
            break
    normalized_asset = asset if asset in {"BTC", "ETH", "SOL"} else ""
    if normalized_asset and duration:
        return f"{normalized_asset} {duration}"
    if normalized_asset:
        return normalized_asset
    if duration:
        return duration
    return "other"


def _replay_event_from_wallet_event(event: WalletActivityEvent) -> TraderReplayEvent:
    return TraderReplayEvent(
        timestamp=_format_timestamp(event.timestamp),
        wallet=event.wallet,
        action=event.action,
        side=event.side,
        asset=event.asset,
        market_id=event.market_id,
        market_title=event.market_title,
        shares=round(float(event.shares or 0.0), 6),
        amount=round(float(event.amount or 0.0), 6),
        price=round(float(event.price or 0.0), 6),
    )


def _compute_statistics(events: list[WalletActivityEvent]) -> dict[str, Any]:
    counts = Counter(event.action for event in events)
    asset_volumes = {"BTC": 0.0, "ETH": 0.0, "SOL": 0.0}
    markets: set[str] = set()
    trade_amounts: list[float] = []
    active_days: set[str] = set()
    timestamps: list[float] = []

    for event in events:
        timestamps.append(event.timestamp)
        if event.market_id:
            markets.add(event.market_id)
        if event.asset in asset_volumes:
            asset_volumes[event.asset] = round(asset_volumes[event.asset] + float(event.amount or 0.0), 6)
        if event.action in TRADE_ACTIONS:
            trade_amounts.append(float(event.amount or 0.0))
        active_days.add(_format_day(event.timestamp))

    first_ts = min(timestamps) if timestamps else 0.0
    last_ts = max(timestamps) if timestamps else 0.0
    largest_trade = round(max(trade_amounts), 6) if trade_amounts else 0.0
    avg_trade_size = round(sum(trade_amounts) / len(trade_amounts), 6) if trade_amounts else 0.0

    return {
        "activity_count": len(events),
        "buy_count": int(counts.get("buy", 0)),
        "sell_count": int(counts.get("sell", 0)),
        "merge_count": int(counts.get("merge", 0)),
        "redeem_count": int(counts.get("redeem", 0)),
        "markets_traded": len(markets),
        "btc_volume": asset_volumes["BTC"],
        "eth_volume": asset_volumes["ETH"],
        "sol_volume": asset_volumes["SOL"],
        "first_activity": _format_timestamp(first_ts) if timestamps else "",
        "last_activity": _format_timestamp(last_ts) if timestamps else "",
        "active_days": len(active_days),
        "avg_trade_size": avg_trade_size,
        "largest_trade": largest_trade,
    }


def _compute_patterns(events: list[WalletActivityEvent], statistics: dict[str, Any]) -> dict[str, Any]:
    asset_counter: Counter[str] = Counter()
    action_counter: Counter[str] = Counter()
    hour_counter: Counter[int] = Counter()
    weekday_counter: Counter[int] = Counter()
    market_type_counter: Counter[str] = Counter()

    for event in events:
        if event.asset:
            asset_counter[event.asset] += 1
        if event.action:
            action_counter[event.action] += 1
        dt = _timestamp_to_dt(event.timestamp)
        hour_counter[dt.hour] += 1
        weekday_counter[dt.weekday()] += 1
        market_type = infer_market_type(event.market_title, event.market_slug, event.asset)
        market_type_counter[market_type] += 1

    trade_count = int(statistics.get("buy_count", 0)) + int(statistics.get("sell_count", 0))
    merge_count = int(statistics.get("merge_count", 0))
    redeem_count = int(statistics.get("redeem_count", 0))
    active_days = max(int(statistics.get("active_days", 0)), 1)
    event_count = len(events)

    sessions = _compute_sessions(events)
    return {
        "preferred_assets": _top_keys(asset_counter, limit=3, exclude={"OTHER", ""}),
        "preferred_actions": _top_keys(action_counter, limit=4),
        "most_active_hour_utc": hour_counter.most_common(1)[0][0] if hour_counter else None,
        "most_active_day_of_week": DAY_NAMES[weekday_counter.most_common(1)[0][0]] if weekday_counter else "",
        "average_events_per_day": round(event_count / active_days, 4),
        "merge_to_trade_ratio": round(merge_count / trade_count, 4) if trade_count else 0.0,
        "redeem_to_trade_ratio": round(redeem_count / trade_count, 4) if trade_count else 0.0,
        "top_market_types": _top_keys(market_type_counter, limit=5),
        "session_count": sessions["session_count"],
        "average_session_length_minutes": sessions["average_session_length_minutes"],
        "largest_session_event_count": sessions["largest_session_event_count"],
    }


def _compute_sessions(events: list[WalletActivityEvent]) -> dict[str, Any]:
    if not events:
        return {
            "session_count": 0,
            "average_session_length_minutes": 0.0,
            "largest_session_event_count": 0,
        }

    gap_seconds = SESSION_GAP_MINUTES * 60
    session_lengths: list[float] = []
    session_sizes: list[int] = []
    session_start = events[0].timestamp
    session_end = events[0].timestamp
    session_size = 1

    for event in events[1:]:
        if event.timestamp - session_end > gap_seconds:
            session_lengths.append(max((session_end - session_start) / 60.0, 0.0))
            session_sizes.append(session_size)
            session_start = event.timestamp
            session_size = 1
        else:
            session_size += 1
        session_end = event.timestamp

    session_lengths.append(max((session_end - session_start) / 60.0, 0.0))
    session_sizes.append(session_size)

    return {
        "session_count": len(session_sizes),
        "average_session_length_minutes": round(sum(session_lengths) / len(session_lengths), 2) if session_lengths else 0.0,
        "largest_session_event_count": max(session_sizes) if session_sizes else 0,
    }


def _build_timeline(
    events: Iterable[TraderReplayEvent],
    *,
    recent_limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = [event.to_timeline_dict() for event in events]
    if recent_limit is not None and recent_limit >= 0:
        rows = rows[-recent_limit:]
    return rows


def _top_keys(counter: Counter[str], *, limit: int, exclude: set[str] | None = None) -> list[str]:
    excluded = exclude or set()
    ordered = [key for key, _count in counter.most_common() if key and key not in excluded]
    return ordered[:limit]


def _format_timestamp(value: float) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_day(value: float) -> str:
    return _timestamp_to_dt(value).date().isoformat()


def _timestamp_to_dt(value: float) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)
