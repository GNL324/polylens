from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE", "API")
REDACTED = "[REDACTED]"


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def redact_value(key: str, value: Any) -> Any:
    return REDACTED if is_secret_key(key) and value not in (None, "") else value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in values.items():
        if is_secret_key(str(key)):
            redacted[str(key)] = REDACTED if value not in (None, "") else value
        elif isinstance(value, Mapping):
            redacted[str(key)] = redact_mapping(value)
        elif isinstance(value, list):
            redacted[str(key)] = [redact_mapping(item) if isinstance(item, Mapping) else item for item in value]
        else:
            redacted[str(key)] = value
    return redacted

