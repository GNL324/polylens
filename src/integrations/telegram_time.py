from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TELEGRAM_TZ = ZoneInfo("America/New_York")


def as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_local_timestamp(dt: datetime, tz: ZoneInfo | str = DEFAULT_TELEGRAM_TZ) -> str:
    """Format a UTC-aware datetime for Telegram display in the requested timezone."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = as_utc_datetime(dt).astimezone(zone)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{local.strftime('%M:%S %p %Z')}"


def format_iso_timestamp_local(value: Any, tz: ZoneInfo | str = DEFAULT_TELEGRAM_TZ) -> str:
    """Parse ISO timestamps and format them for Telegram in local time."""
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:32] if len(text) <= 32 else text[:31].rstrip() + "..."
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = as_utc_datetime(parsed).astimezone(zone)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%Y-%m-%d')} {hour}:{local.strftime('%M %p %Z')}"
