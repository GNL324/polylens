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
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
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
    window_type: str | None = None
    reference_time: str | None = None
    close_time: str | None = None
    comparator_baseline: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_crypto_market_text(text: str, close_time: str | None = None) -> ParsedCryptoMarket:
    normalized = f" {normalize_text(text)} "
    asset_symbol = _detect_asset(normalized, text)
    asset_name = ASSETS[asset_symbol]["name"] if asset_symbol else None
    direction = _detect_direction(normalized)
    if direction is None and normalized.rstrip().endswith(" up"):
        direction = "up"
    if direction is None and normalized.rstrip().endswith(" down"):
        direction = "down"
    prices = _extract_prices(text)
    lower_bound, upper_bound = (prices[0], prices[1]) if len(prices) >= 2 and direction == "between" else (None, None)
    target_price = None if lower_bound is not None else (prices[0] if prices else None)
    parsed_close_time = _datetime_from_close_time(close_time)
    expiry_date = _detect_expiry_date(normalized) or (parsed_close_time[:10] if parsed_close_time else None)
    window_type = _detect_window_type(normalized, text, parsed_close_time)
    reference_time = _detect_reference_time(normalized, text, expiry_date)
    comparator_baseline = _detect_baseline(normalized, direction, target_price)
    market_type = _detect_market_type(normalized, direction, window_type, comparator_baseline)
    matched_terms = []
    if asset_symbol:
        matched_terms.extend([asset_symbol.lower(), asset_name.lower()])
    if target_price is not None:
        matched_terms.append(_format_price(target_price))
    if lower_bound is not None and upper_bound is not None:
        matched_terms.extend([_format_price(lower_bound), _format_price(upper_bound)])
    for value in (direction, expiry_date, window_type, comparator_baseline, market_type):
        if value:
            matched_terms.append(value)
    return ParsedCryptoMarket(
        asset_symbol,
        asset_name,
        target_price,
        lower_bound,
        upper_bound,
        direction,
        expiry_date,
        market_type,
        matched_terms,
        text,
        window_type=window_type,
        reference_time=reference_time,
        close_time=parsed_close_time,
        comparator_baseline=comparator_baseline,
    )


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
        if any(prefix in upper or f"-{symbol}" in upper for prefix in meta["ticker_prefixes"]):
            return symbol
        if any(f" {normalize_text(term)} " in normalized for term in meta["terms"]):
            return symbol
    return None


def _extract_prices(text: str) -> list[float]:
    prices: list[float] = []
    for match in re.finditer(r"(\$)?\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(k|m))?", text, re.IGNORECASE):
        has_dollar = bool(match.group(1))
        suffix = (match.group(3) or "").lower()
        if not has_dollar and suffix not in {"k", "m"}:
            continue
        value = float(match.group(2).replace(",", ""))
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
    if any(term in normalized for term in (" up or down ", " higher or lower ")):
        return None
    if any(term in normalized for term in (" touch ", " touches ", " hit ", " hits ", " reach ", " reaches ")):
        return "touches"
    if any(term in normalized for term in (" above ", " over ", " greater than ", " at least ")):
        return "above"
    if any(term in normalized for term in (" below ", " under ", " less than ")):
        return "below"
    if any(term in normalized for term in (" higher ", " up ", " increases ", " rise ", " rises ")):
        return "up"
    if any(term in normalized for term in (" lower ", " down ", " decreases ", " fall ", " falls ")):
        return "down"
    return None


def _detect_window_type(normalized: str, raw_text: str, close_time: str | None) -> str | None:
    if any(term in normalized for term in (" hourly ", " hour ", " 1h ", " 1 hour ")) or re.search(r"\b\d{1,2}\s*:\s*\d{2}\s*(am|pm)?\b", raw_text, re.IGNORECASE):
        return "hourly"
    if any(term in normalized for term in (" weekly ", " week ", " this week ")):
        return "weekly"
    if any(term in normalized for term in (" daily ", " today ", " tonight ", " by close ", " previous close ", " open price ", " on ")):
        return "daily"
    return "daily" if close_time else None


def _detect_reference_time(normalized: str, raw_text: str, expiry_date: str | None) -> str | None:
    range_match = re.search(r"\b(\d{1,2}:\d{2}\s*(?:am|pm)?)\s*(?:-|to)\s*(\d{1,2}:\d{2}\s*(?:am|pm)?)\b", raw_text, re.IGNORECASE)
    if range_match and expiry_date:
        return f"{expiry_date} {range_match.group(1)}-{range_match.group(2)}"
    single_match = re.search(r"\b(\d{1,2}:\d{2}\s*(?:am|pm)?)\b", raw_text, re.IGNORECASE)
    if single_match and expiry_date:
        return f"{expiry_date} {single_match.group(1)}"
    return expiry_date


def _detect_baseline(normalized: str, direction: str | None, target_price: float | None) -> str | None:
    if target_price is not None:
        return "fixed target"
    if " previous close " in normalized or " prior close " in normalized:
        return "previous close"
    if " open price " in normalized or " opening price " in normalized:
        return "open price"
    if direction in {"up", "down"}:
        return "previous close"
    return None


def _detect_market_type(normalized: str, direction: str | None, window_type: str | None, baseline: str | None) -> str | None:
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
    if direction in {"up", "down"} and baseline in {"previous close", "open price"}:
        return f"{window_type or 'daily'} up/down"
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
    return f"{year:04d}-{month:02d}-{day:02d}"


def _datetime_from_close_time(close_time: str | None) -> str | None:
    if not close_time:
        return None
    try:
        return datetime.fromisoformat(close_time.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _format_price(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
