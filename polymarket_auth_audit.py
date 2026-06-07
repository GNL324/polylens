from __future__ import annotations

import enum
import json
import logging
import types
from collections.abc import Mapping
from typing import Any

from src.adapters.polymarket_live import (
    PolymarketLiveAdapter,
    probe_sdk,
)

# Placeholder token for signing probe only; no order is sent.
_DEFAULT_PROBE_TOKEN_ID = "13915689317269078219168496739008737517740566192006337297676041270492637394586"

_SENSITIVE_VALUE_KEYS = frozenset(
    {
        "private_key",
        "api_key",
        "api_secret",
        "api_passphrase",
        "passphrase",
        "secret",
        "signature",
        "signed_payload",
        "raw_payload",
        "payload",
    }
)

_SAFE_METADATA_KEYS = frozenset(
    {
        "has_signature",
        "signed_order_created",
        "order_type",
        "token_id_present",
        "price_present",
        "size_present",
        "side",
    }
)


def main() -> None:
    logging.disable(logging.CRITICAL)
    print(json.dumps(build_auth_audit(), indent=2, sort_keys=True))


def safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return "<bytes>"
    if isinstance(value, enum.Enum):
        return _normalize_side(value.name) or type(value).__name__
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_sensitive_key(key_str):
                continue
            out[key_str] = safe_json(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [safe_json(item) for item in value]
    if isinstance(value, types.MappingProxyType):
        return safe_json(dict(value))
    if hasattr(value, "name") and hasattr(value, "value"):
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return _normalize_side(name) or name
    if hasattr(value, "__dict__"):
        return safe_json(
            {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_") and not _is_sensitive_key(key)
            }
        )
    return type(value).__name__


def summarize_signed_order(signed_order: Any) -> dict[str, Any]:
    if signed_order is None:
        return {
            "signed_order_created": False,
            "order_type": None,
            "has_signature": False,
            "side": None,
            "token_id_present": False,
            "price_present": False,
            "size_present": False,
        }
    return {
        "signed_order_created": True,
        "order_type": type(signed_order).__name__,
        "has_signature": _field_present(signed_order, "signature"),
        "side": _extract_side_string(signed_order),
        "token_id_present": _field_present(signed_order, "token_id", "tokenId"),
        "price_present": _field_present(signed_order, "price"),
        "size_present": _field_present(signed_order, "size", "original_size", "originalSize"),
    }


def sanitize_dry_run_signing(dry_run_signing: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(dry_run_signing)
    raw_signed_order = sanitized.pop("signed_order", None)
    if raw_signed_order is not None or sanitized.get("ok"):
        sanitized["signed_order"] = summarize_signed_order(raw_signed_order)
    return sanitized


def build_auth_audit(*, probe_token_id: str = _DEFAULT_PROBE_TOKEN_ID) -> dict[str, Any]:
    adapter = PolymarketLiveAdapter()
    creds = adapter.credentials_present()
    sdk_probe = probe_sdk()
    sdk_available = bool(sdk_probe.get("sdk_available"))

    credential_checks = {
        "POLYMARKET_PRIVATE_KEY_present": bool(creds.get("private_key_present")),
        "POLYMARKET_API_KEY_present": bool(creds.get("api_key_present")),
        "POLYMARKET_API_SECRET_present": bool(creds.get("api_secret_present")),
        "POLYMARKET_API_PASSPHRASE_present": bool(creds.get("api_passphrase_present")),
        "POLYMARKET_FUNDER_present": bool(creds.get("funder_detected_not_printed")),
        "POLYMARKET_SIGNATURE_TYPE_present": creds.get("signature_type") is not None,
    }

    clob_client_init: dict[str, Any] = {
        "ok": False,
        "attempted": False,
        "reason": "credentials_missing",
    }
    balance_allowance: dict[str, Any] = {
        "ok": False,
        "status": "not_ready",
        "reason": "credentials_missing",
        "attempted": False,
        "auth_valid": False,
        "balance_allowance_attempts": [],
        "first_successful_asset_type": None,
        "balance_summary": None,
    }
    dry_run_signing: dict[str, Any] = {
        "ok": False,
        "status": "not_ready",
        "reason": "credentials_missing",
        "attempted": False,
        "order_endpoint_called": False,
        "sent": False,
    }

    if creds.get("ok"):
        clob_client_init = adapter.try_init_client()
        clob_client_init["attempted"] = True
        balance_allowance = adapter.get_balance_allowance()
        balance_allowance["attempted"] = True
        dry_run_signing = adapter.dry_run_order(
            token_id=str(probe_token_id),
            price=0.01,
            size=1.0,
            tick_size="0.01",
            neg_risk=False,
        )
        dry_run_signing["attempted"] = True
        dry_run_signing = sanitize_dry_run_signing(dry_run_signing)

    auth_valid = bool(balance_allowance.get("auth_valid")) if balance_allowance.get("attempted") else False

    failed_checks: list[str] = []
    if not creds.get("ok"):
        failed_checks.append("polymarket_credentials_missing")
    for name, ok in credential_checks.items():
        if not ok and name != "POLYMARKET_FUNDER_present":
            failed_checks.append(name)
    if not sdk_available:
        failed_checks.append("polymarket_sdk_not_available")
    if clob_client_init.get("attempted") and not clob_client_init.get("ok"):
        failed_checks.append(clob_client_init.get("reason", "clob_client_init_failed"))
    if balance_allowance.get("attempted") and not balance_allowance.get("ok"):
        failed_checks.append(balance_allowance.get("reason", "balance_allowance_not_ready"))
    if dry_run_signing.get("attempted") and not dry_run_signing.get("ok"):
        failed_checks.append(dry_run_signing.get("reason", "dry_run_signing_failed"))

    payload = {
        "status": "ready" if not failed_checks else "not_ready",
        "failed_checks": failed_checks,
        "auth_valid": auth_valid,
        "credentials": credential_checks,
        "POLYMARKET_SIGNATURE_TYPE": creds.get("signature_type"),
        "chain_id": creds.get("chain_id"),
        "clob_host": creds.get("clob_host"),
        "sdk_available": sdk_available,
        "sdk_package": sdk_probe.get("sdk_package"),
        "sdk_imports": sdk_probe.get("imports", {}),
        "clob_client_init": clob_client_init,
        "balance_allowance": balance_allowance,
        "dry_run_signing": dry_run_signing,
        "post_order_called": bool(getattr(adapter, "_post_order_called", False)),
        "sent": False,
    }
    return safe_json(payload)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered in _SAFE_METADATA_KEYS:
        return False
    if lowered.endswith("_present") or lowered.endswith("_type") or lowered.endswith("_detected_not_printed"):
        return False
    return lowered in _SENSITIVE_VALUE_KEYS


def _field_present(value: Any, *names: str) -> bool:
    for name in names:
        if isinstance(value, Mapping):
            if name in value and value[name] not in (None, ""):
                return True
            continue
        if getattr(value, name, None) not in (None, ""):
            return True
    return False


def _normalize_side(value: str | None) -> str | None:
    if not value:
        return None
    upper = value.upper()
    if upper in {"BUY", "SELL"}:
        return upper
    return None


def _extract_side_string(value: Any) -> str | None:
    side = value.get("side") if isinstance(value, Mapping) else getattr(value, "side", None)
    if side is None:
        return None
    if isinstance(side, enum.Enum):
        return _normalize_side(side.name)
    if isinstance(side, int):
        return "BUY" if side == 0 else "SELL" if side == 1 else None
    if isinstance(side, str):
        return _normalize_side(side)
    name = getattr(side, "name", None)
    if isinstance(name, str):
        normalized = _normalize_side(name)
        if normalized:
            return normalized
    side_value = getattr(side, "value", None)
    if side_value == 0:
        return "BUY"
    if side_value == 1:
        return "SELL"
    return None


if __name__ == "__main__":
    main()
