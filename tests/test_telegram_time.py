from __future__ import annotations

from datetime import datetime, timezone

from src.integrations.telegram_time import (
    DEFAULT_TELEGRAM_TZ,
    format_iso_timestamp_local,
    format_local_timestamp,
)


def test_format_local_timestamp_uses_edt_during_daylight_saving():
    dt = datetime(2026, 6, 26, 12, 34, 56, tzinfo=timezone.utc)

    assert format_local_timestamp(dt) == "8:34:56 AM EDT"


def test_format_local_timestamp_uses_est_during_standard_time():
    dt = datetime(2026, 1, 15, 17, 13, 1, tzinfo=timezone.utc)

    assert format_local_timestamp(dt) == "12:13:01 PM EST"


def test_format_local_timestamp_respects_custom_timezone():
    dt = datetime(2026, 6, 26, 12, 34, 56, tzinfo=timezone.utc)

    assert format_local_timestamp(dt, tz="America/Los_Angeles") == "5:34:56 AM PDT"


def test_format_iso_timestamp_local_converts_z_suffix():
    assert format_iso_timestamp_local("2026-06-26T12:34:56Z") == "2026-06-26 8:34 AM EDT"


def test_default_timezone_is_new_york():
    assert str(DEFAULT_TELEGRAM_TZ) == "America/New_York"
