from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.analysis.market_normalization import normalize_text

REJECTION_REASON = "incompatible market types"
OUTRIGHT_TYPES = {"outright", "championship_winner", "series_winner"}
INCOMPATIBLE_WITH_OUTRIGHT = {"player_prop", "game_total", "spread", "cross_category", "multileg"}
BUNDLE_TICKER_PREFIXES = ("KXMVECROSSCATEGORY", "KXMVESPORTSMULTIGAMEEXTENDED", "KXMVE")


@dataclass(frozen=True)
class MarketTypeValidation:
    accepted: bool
    polymarket_product_type: str
    kalshi_product_type: str
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_market_type_compatibility(polymarket_market: dict[str, Any], kalshi_market: dict[str, Any], parsed_polymarket: Any | None = None, parsed_kalshi: Any | None = None) -> MarketTypeValidation:
    pm_type = classify_polymarket_product(polymarket_market, parsed_polymarket)
    kalshi_type = classify_kalshi_product(kalshi_market, parsed_kalshi)
    if _incompatible(pm_type, kalshi_type):
        return MarketTypeValidation(False, pm_type, kalshi_type, REJECTION_REASON)
    return MarketTypeValidation(True, pm_type, kalshi_type)


def classify_polymarket_product(market: dict[str, Any], parsed: Any | None = None) -> str:
    text = _market_text(market, ("title", "question", "slug", "eventSlug", "category", "outcome"))
    parsed_type = getattr(parsed, "market_type", None)
    if _has_any(text, ("player", "points", "rebounds", "assists", "goalscorer", "strikeouts", "touchdowns")):
        return "player_prop"
    if _has_any(text, ("total points", "total runs", "over ", "under ", "o/u")):
        return "game_total"
    if _has_any(text, ("spread", "wins by over", "wins by more than", "+1.5", "-1.5")):
        return "spread"
    if parsed_type in {"championship_winner", "series_winner"}:
        return parsed_type
    if _has_any(text, ("win the 2026 nba finals", "win the nba finals", "win the finals", "nba finals", "champion", "championship", "world series", "stanley cup", "super bowl")):
        return "championship_winner"
    if parsed_type == "game_winner" or _has_any(text, (" beat ", " vs ", " at ", "moneyline")):
        return "game_winner"
    if _has_any(text, ("winner", "outright")):
        return "outright"
    return parsed_type or "unknown"


def classify_kalshi_product(market: dict[str, Any], parsed: Any | None = None) -> str:
    text = _market_text(market, ("title", "subtitle", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary", "ticker", "event_ticker", "series_ticker", "category"))
    ticker = str(market.get("ticker") or market.get("event_ticker") or market.get("series_ticker") or "").upper()
    parsed_type = getattr(parsed, "market_type", None)
    if ticker.startswith("KXMVECROSSCATEGORY") or "crosscategory" in text or "cross category" in text:
        return "cross_category"
    if ticker.startswith(BUNDLE_TICKER_PREFIXES) or _has_any(text, ("multigame", "multi game", "parlay", "same game", "associated markets", "associated events")):
        return "multileg"
    if _has_any(text, ("player", "points", "rebounds", "assists", "goalscorer", "strikeouts", "touchdowns")):
        return "player_prop"
    if _has_any(text, ("total points", "total runs", "over ", "under ", "runs scored", "points scored")):
        return "game_total"
    if _has_any(text, ("spread", "wins by over", "wins by more than", "+1.5", "-1.5")):
        return "spread"
    if parsed_type in {"championship_winner", "series_winner"}:
        return parsed_type
    if _has_any(text, ("win the nba finals", "nba finals", "champion", "championship", "world series", "stanley cup", "super bowl")):
        return "championship_winner"
    if parsed_type == "game_winner" or _has_any(text, (" beat ", " vs ", " at ", "moneyline")):
        return "game_winner"
    if _has_any(text, ("winner", "outright")):
        return "outright"
    return parsed_type or "unknown"


def validation_diagnostic(polymarket_market: dict[str, Any], kalshi_market: dict[str, Any], validation: MarketTypeValidation, parsed_polymarket: Any | None = None, parsed_kalshi: Any | None = None) -> dict[str, Any]:
    return {
        "polymarket_title": str(polymarket_market.get("title") or polymarket_market.get("question") or polymarket_market.get("slug") or ""),
        "kalshi_title": str(kalshi_market.get("title") or kalshi_market.get("subtitle") or kalshi_market.get("ticker") or ""),
        "parser_type": "market_type_validation",
        "match_type": f"{validation.polymarket_product_type}_vs_{validation.kalshi_product_type}",
        "accepted": validation.accepted,
        "reason": validation.rejection_reason or "market types compatible",
        "rejection_reason": validation.rejection_reason,
        "parsed_polymarket": _parsed_dict(parsed_polymarket),
        "parsed_kalshi": _parsed_dict(parsed_kalshi),
        "confidence": 0.0 if not validation.accepted else 1.0,
        "market_type_validation": validation.to_dict(),
    }


def _incompatible(left: str, right: str) -> bool:
    if left == "championship_winner" and right in {"multileg", "cross_category"}:
        return True
    if right == "championship_winner" and left in {"multileg", "cross_category"}:
        return True
    if left in OUTRIGHT_TYPES and right in INCOMPATIBLE_WITH_OUTRIGHT:
        return True
    if right in OUTRIGHT_TYPES and left in INCOMPATIBLE_WITH_OUTRIGHT:
        return True
    return False


def _market_text(market: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts = [str(market.get(field) or "") for field in fields]
    custom = market.get("custom_strike")
    if isinstance(custom, dict):
        parts.extend(str(value or "") for value in custom.values())
    return f" {normalize_text(' '.join(parts))} "


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(normalize_text(term) in text for term in terms)


def _parsed_dict(parsed: Any | None) -> dict[str, Any]:
    if parsed is None:
        return {}
    if hasattr(parsed, "to_dict"):
        return parsed.to_dict()
    if isinstance(parsed, dict):
        return parsed
    return {}
