from __future__ import annotations

import re
from typing import Any

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

KNOWN_SYNTHETIC_WALLETS = frozenset(
    {
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
)


def normalize_wallet(wallet: str) -> str:
    return str(wallet or "").strip().lower()


def is_repeated_character_wallet(wallet: str) -> bool:
    normalized = normalize_wallet(wallet)
    if not WALLET_RE.match(normalized):
        return False
    body = normalized[2:]
    return len(set(body)) == 1


def is_synthetic_wallet(wallet: str, *, metadata: dict[str, Any] | None = None) -> bool:
    normalized = normalize_wallet(wallet)
    if not normalized:
        return False
    if normalized in KNOWN_SYNTHETIC_WALLETS:
        return True
    if metadata:
        if metadata.get("synthetic") is True:
            return True
        if str(metadata.get("fixture") or "").lower() in {"true", "1", "yes"}:
            return True
        if str(metadata.get("source") or "").startswith("test_"):
            return True
    return False


def is_synthetic_seed_wallet(wallet: str, *, metadata: dict[str, Any] | None = None) -> bool:
    if is_synthetic_wallet(wallet, metadata=metadata):
        return True
    return is_repeated_character_wallet(wallet)


def synthetic_wallet_reason(wallet: str, *, metadata: dict[str, Any] | None = None) -> str:
    normalized = normalize_wallet(wallet)
    if normalized in KNOWN_SYNTHETIC_WALLETS:
        return "known synthetic fixture wallet"
    if is_repeated_character_wallet(normalized):
        return "repeated-character synthetic wallet"
    if metadata and metadata.get("synthetic"):
        return "metadata marked synthetic"
    return "synthetic wallet"


def filter_production_wallets(wallets: list[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for wallet in wallets:
        normalized = normalize_wallet(wallet)
        if not normalized or normalized in seen or is_synthetic_wallet(normalized):
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def partition_wallets(wallets: list[str]) -> tuple[list[str], list[str]]:
    real: list[str] = []
    synthetic: list[str] = []
    for wallet in wallets:
        normalized = normalize_wallet(wallet)
        if not normalized:
            continue
        if is_synthetic_wallet(normalized):
            synthetic.append(normalized)
        else:
            real.append(normalized)
    return real, synthetic


def wallet_production_flags(wallet: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    synthetic = is_synthetic_wallet(wallet, metadata=metadata)
    return {
        "wallet": normalize_wallet(wallet),
        "synthetic": synthetic,
        "production_eligible": not synthetic,
        "reason": synthetic_wallet_reason(wallet, metadata=metadata) if synthetic else "real wallet",
    }
