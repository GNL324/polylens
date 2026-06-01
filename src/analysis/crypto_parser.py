from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.analysis.market_normalization import normalize_text

ASSETS = {
    "BTC": {"name": "Bitcoin", "terms": ["bitcoin", "btc", "xbt"], "ticker_prefixes": ["KXBTC"]},
    "ETH": {"name": "Ethereum", "terms": ["ethereum", "ether", "eth"], "ticker_prefixes": ["KXETH"]},
    "SOL": {"name": "Solana", "terms": ["solana", "sol"], "ticker_prefixes": ["KXSOL"]},
    "XRP": {"name": "XRP", "terms": ["xrp", "ripple"], "ticker_prefixes": ["KXXRP"]},
    "DOGE": {"name": "Dogecoin", "terms": ["dogecoin", "doge"], "ticker_prefixes": ["KXDOGE"]},
    "ADA": {"name": "Cardano", "terms": ["cardano", "ada"], "ticker_prefixes": ["KXADA"]},
}
MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


@dataclass(frozen=True)
class ParsedCryptoMarket:
    asset_symbol: str | None
    asset_name: str | None
    target_price: float | None
    lower_bound: float | None
    upper_bound: float | None
    direction: str | None
    expiry_date: str | None
    market_type: str | None
    matched_terms: list[str]
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_crypto_market_text(text: str, close_time: str | None = None) -> ParsedCryptoMarket:
    normalized = f" {normalize_text(text)} "
    asset_symbol = _detect_asset(normalized, text)
    asset_name = ASSETS[asset_symbol]["name"] if asset_symbol else None
    prices = _extract_prices(text)
    lower_bound, upper_bound = (prices[0], prices[1]) if len(prices) >= 2 and _detect_direction(normalized) == "between" else (None, None)
    target_price = None if lower_bound is not None else (prices[0] if prices else None)
    direction = _detect_direction(normalized)
    expiry_date = _detect_expiry_date(normalized) or _date_from_close_time(close_time)
    market_type = _detect_market_type(normalized, direction)
    matched_terms = []
    if asset_symbol:
        matched_terms.extend([asset_symbol.lower(), asset_name.lower()])
    if target_price is not None:
        matched_terms.append(_format_price(target_price))
    if lower_bound is not None and upper_bound is not None:
        matched_terms.extend([_format_price(lower_bound), _format_price(upper_bound)])
    if direction:
        matched_terms.append(direction)
    if expiry_date:
        matched_terms.append(expiry_date)
    if market_type:
        matched_terms.append(market_type)
    return ParsedCryptoMarket(asset_symbol, asset_name, target_price, lower_bound, upper_bound, direction, expiry_date, market_type, matched_terms, text)


def parse_market_record(record: dict[str, Any], source: str) -> ParsedCryptoMarket:
    if source == "kalshi":
        fields = ("title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary", "ticker", "event_ticker", "series_ticker")
        close_time = record.get("close_time") or record.get("expected_expiration_time") or record.get("expiration_time")
    else:
        fields = ("title", "slug", "eventSlug", "outcome")
        close_time = None
    text = " ".join(str(record.get(field) or "") for field in fields)
    return parse_crypto_market_text(text, close_time=str(close_time) if close_time else None)


def _detect_asset(normalized: str, raw_text: str) -> str | None:
    upper = raw_text.upper()
    for symbol, meta in ASSETS.items():
        if any(upper.startswith(prefix) or f" {prefix}" in upper or f"-{symbol}" in upper for prefix in meta["ticker_prefixes"]):
            return symbol
        if any(f" {normalize_text(term)} " in normalized for term in meta["terms"]):
            return symbol
    return None


def _extract_prices(text: str) -> list[float]:
    prices: list[float] = []
    for match in re.finditer(r"\$?\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(k|m))?", text, re.IGNORECASE):
        value = float(match.group(1).replace(",", ""))
        suffix = (match.group(2) or "").lower()
        if suffix == "k":
            value *= 1_000
        elif suffix == "m":
            value *= 1_000_000
        if value >= 0.01:
            prices.append(value)
    return prices


def _detect_direction(normalized: str) -> str | None:
    if " between " in normalized:
        return "between"
    if any(term in normalized for term in (" touch ", " touches ", " hit ", " hits ", " reach ", " reaches ")):
        return "touches"
    if any(term in normalized for term in (" above ", " over ", " greater than ", " at least ")):
        return "above"
    if any(term in normalized for term in (" below ", " under ", " less than ")):
        return "below"
    return None


def _detect_market_type(normalized: str, direction: str | None) -> str | None:
    if " all time high " in normalized or " ath " in normalized:
        return "all-time high"
    if " daily close " in normalized or " close daily " in normalized:
        return "daily close"
    if " weekly close " in normalized or " week close " in normalized:
        return "weekly close"
    if " monthly close " in normalized or " month close " in normalized:
        return "monthly close"
    if direction == "between":
        return "range"
    if direction in {"above", "below", "touches"}:
        return "price target"
    return None


def _detect_expiry_date(normalized: str) -> str | None:
    match = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\s+(\d{1,2})(?:\s+(20\d{2}))?\b", normalized)
    if not match:
        return None
    month = MONTHS[match.group(1)]
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else 2026
    try:
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def _date_from_close_time(close_time: str | None) -> str | None:
    if not close_time:
        return None
    try:
        return datetime.fromisoformat(close_time.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return None


def _format_price(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
