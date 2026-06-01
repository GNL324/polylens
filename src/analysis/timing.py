from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any


def _timestamp_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 10_000_000_000:
        numeric = numeric / 1000
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def summarize_timing(trades: list[dict[str, Any]]) -> dict[str, Any]:
    hours: Counter[int] = Counter()
    weekdays: Counter[str] = Counter()
    for trade in trades:
        dt = _timestamp_to_dt(trade.get("timestamp"))
        if not dt:
            continue
        hours[dt.hour] += 1
        weekdays[dt.strftime("%A")] += 1
    most_active_hour = hours.most_common(1)[0][0] if hours else None
    most_active_weekday = weekdays.most_common(1)[0][0] if weekdays else None
    return {
        "trading_by_hour_utc": dict(sorted(hours.items())),
        "trading_by_weekday_utc": dict(weekdays.most_common()),
        "most_active_periods": {
            "hour_utc": most_active_hour,
            "weekday_utc": most_active_weekday,
        },
    }
