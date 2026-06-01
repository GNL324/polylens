from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from src.analysis.crypto_parser import ParsedCryptoMarket, parse_market_record
from src.analysis.market_normalization import normalize_text

PRICE_TOLERANCE = 0.01
EXPIRY_WINDOW_DAYS = 2
TIME_WINDOW_MINUTES = {"hourly": 90, "daily": 36 * 60, "weekly": 8 * 24 * 60}


@dataclass(frozen=True)
class MatchDiagnostic:
    polymarket_title: str
    kalshi_title: str
    parser_type: str
    match_type: str
    accepted: bool
    reason: str
    parsed_polymarket: dict[str, Any]
    parsed_kalshi: dict[str, Any]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def structured_crypto_candidates(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int = 10) -> list[dict[str, Any]]:
    candidates, _diagnostics = structured_crypto_candidates_with_diagnostics(polymarket_markets, kalshi_markets, max_candidates=max_candidates)
    return candidates


def structured_crypto_candidates_with_diagnostics(polymarket_markets: list[dict[str, Any]], kalshi_markets: list[dict[str, Any]], max_candidates: int = 10, max_diagnostics: int = 250) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    parsed_pm = [(market, parse_market_record(market, "polymarket")) for market in polymarket_markets]
    parsed_kalshi = [(market, parse_market_record(market, "kalshi")) for market in kalshi_markets]
    for pm_market, pm_parsed in parsed_pm:
        if not pm_parsed.asset_symbol:
            continue
        for kalshi_market, kalshi_parsed in parsed_kalshi:
            candidate, diagnostic = score_structured_crypto_pair_with_diagnostic(pm_market, pm_parsed, kalshi_market, kalshi_parsed)
            if diagnostic and (diagnostic["accepted"] or len(diagnostics) < max_diagnostics):
                diagnostics.append(diagnostic)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=lambda item: (item["similarity_score"], len(item["shared_keywords_entities"])), reverse=True)
    return candidates[:max_candidates], diagnostics


def score_structured_crypto_pair(pm_market: dict[str, Any], pm: ParsedCryptoMarket, kalshi_market: dict[str, Any], kalshi: ParsedCryptoMarket) -> dict[str, Any] | None:
    candidate, _diagnostic = score_structured_crypto_pair_with_diagnostic(pm_market, pm, kalshi_market, kalshi)
    return candidate


def score_structured_crypto_pair_with_diagnostic(pm_market: dict[str, Any], pm: ParsedCryptoMarket, kalshi_market: dict[str, Any], kalshi: ParsedCryptoMarket) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    pm_title = str(pm_market.get("title") or pm_market.get("slug") or pm_market.get("conditionId") or "")
    kalshi_title = str(kalshi_market.get("title") or kalshi_market.get("ticker") or "")

    def reject(reason: str, confidence: float = 0.0) -> tuple[None, dict[str, Any]]:
        return None, _diagnostic(pm_title, kalshi_title, "crypto", _match_type(pm, kalshi), False, reason, pm, kalshi, confidence)

    if not pm.asset_symbol or not kalshi.asset_symbol:
        return reject("missing crypto asset")
    if pm.asset_symbol != kalshi.asset_symbol:
        return reject(f"asset mismatch: {pm.asset_symbol} vs {kalshi.asset_symbol}")
    if not _contract_type_compatible(pm, kalshi):
        return reject("fixed-target and up/down contracts are not provably equivalent", 0.35)
    if not _directions_compatible(pm.direction, kalshi.direction):
        return reject(f"direction mismatch: {pm.direction} vs {kalshi.direction}", 0.25)
    if not _price_or_baseline_compatible(pm, kalshi):
        return reject("target price or comparator baseline mismatch", 0.45)
    if not _window_compatible(pm, kalshi):
        return reject("close windows do not overlap", 0.55)
    if pm.market_type and kalshi.market_type and not _market_types_compatible(pm.market_type, kalshi.market_type):
        return reject(f"market type mismatch: {pm.market_type} vs {kalshi.market_type}", 0.45)

    score = 0.35
    reasons = [f"asset match: {pm.asset_symbol}"]
    shared_terms = {pm.asset_symbol.lower(), (pm.asset_name or pm.asset_symbol).lower()}
    if pm.direction and kalshi.direction:
        score += 0.14
        shared_terms.add(pm.direction if pm.direction == kalshi.direction else f"{pm.direction}/{kalshi.direction}")
        reasons.append(f"direction compatible: {pm.direction} vs {kalshi.direction}")
    if _is_up_down(pm) and _is_up_down(kalshi):
        score += 0.20
        shared_terms.add(pm.comparator_baseline or "baseline")
        reasons.append(f"baseline compatible: {pm.comparator_baseline} vs {kalshi.comparator_baseline}")
    elif _targets_compatible(pm, kalshi):
        score += 0.24
        shared_terms.update(_target_terms(pm))
        reasons.append("target price compatible")
    if _window_compatible(pm, kalshi):
        score += 0.18
        if pm.window_type:
            shared_terms.add(pm.window_type)
        reasons.append(f"window compatible: {pm.window_type} vs {kalshi.window_type}")
    if pm.market_type and kalshi.market_type and _market_types_compatible(pm.market_type, kalshi.market_type):
        score += 0.10
        shared_terms.add(pm.market_type)
        reasons.append(f"market type compatible: {pm.market_type} vs {kalshi.market_type}")
    if score < 0.72:
        return reject("structured crypto score below threshold", score)

    score = min(score, 0.99)
    confidence = "high" if score >= 0.9 else "medium" if score >= 0.78 else "low"
    candidate = {
        "normalized_title": normalize_text(pm_title),
        "polymarket_id": str(pm_market.get("conditionId") or pm_market.get("slug") or pm_title),
        "polymarket_title": pm_title,
        "kalshi_ticker": str(kalshi_market.get("ticker") or kalshi_market.get("market_ticker") or kalshi_title),
        "kalshi_title": kalshi_title,
        "shared_keywords_entities": sorted(shared_terms),
        "sport_league_category_guess": {"category": "Crypto", "league": None},
        "similarity_score": round(score, 4),
        "confidence_band": confidence,
        "reason": "; ".join(reasons),
        "structured_match": {"polymarket": pm.to_dict(), "kalshi": kalshi.to_dict()},
    }
    return candidate, _diagnostic(pm_title, kalshi_title, "crypto", _match_type(pm, kalshi), True, candidate["reason"], pm, kalshi, score)


def _diagnostic(pm_title: str, kalshi_title: str, parser_type: str, match_type: str, accepted: bool, reason: str, pm: ParsedCryptoMarket, kalshi: ParsedCryptoMarket, confidence: float) -> dict[str, Any]:
    return MatchDiagnostic(pm_title, kalshi_title, parser_type, match_type, accepted, reason, pm.to_dict(), kalshi.to_dict(), round(confidence, 4)).to_dict()


def _match_type(pm: ParsedCryptoMarket, kalshi: ParsedCryptoMarket) -> str:
    if _is_up_down(pm) or _is_up_down(kalshi):
        return "short_window_crypto"
    return "crypto_price_target"


def _directions_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    aliases = {"higher": "up", "lower": "down"}
    left = aliases.get(left, left)
    right = aliases.get(right, right)
    return left == right or {left, right} == {"above", "touches"}


def _contract_type_compatible(left: ParsedCryptoMarket, right: ParsedCryptoMarket) -> bool:
    if _is_up_down(left) or _is_up_down(right):
        return _is_up_down(left) and _is_up_down(right)
    return True


def _price_or_baseline_compatible(left: ParsedCryptoMarket, right: ParsedCryptoMarket) -> bool:
    if _is_up_down(left) and _is_up_down(right):
        return bool(left.comparator_baseline and right.comparator_baseline and left.comparator_baseline == right.comparator_baseline)
    return _targets_compatible(left, right)


def _is_up_down(parsed: ParsedCryptoMarket) -> bool:
    return parsed.direction in {"up", "down"} and parsed.comparator_baseline in {"previous close", "open price"}


def _targets_compatible(left: ParsedCryptoMarket, right: ParsedCryptoMarket) -> bool:
    if left.direction == "between" or right.direction == "between":
        if None in (left.lower_bound, left.upper_bound, right.lower_bound, right.upper_bound):
            return False
        return _close(left.lower_bound, right.lower_bound) and _close(left.upper_bound, right.upper_bound)
    if left.target_price is None or right.target_price is None:
        return False
    return _close(left.target_price, right.target_price)


def _window_compatible(left: ParsedCryptoMarket, right: ParsedCryptoMarket) -> bool:
    if left.window_type and right.window_type and left.window_type != right.window_type:
        return False
    if left.close_time and right.close_time:
        return _close_times_overlap(left.close_time, right.close_time, left.window_type or right.window_type)
    return _expiry_compatible(left.expiry_date, right.expiry_date)


def _close(left: float | None, right: float | None) -> bool:
    if left is None or right is None or left == 0:
        return False
    return abs(left - right) / left <= PRICE_TOLERANCE


def _expiry_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_date = date.fromisoformat(left)
        right_date = date.fromisoformat(right)
    except ValueError:
        return False
    return abs((left_date - right_date).days) <= EXPIRY_WINDOW_DAYS


def _close_times_overlap(left: str, right: str, window_type: str | None) -> bool:
    try:
        left_dt = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(right.replace("Z", "+00:00"))
    except ValueError:
        return False
    minutes = abs((left_dt - right_dt).total_seconds()) / 60
    return minutes <= TIME_WINDOW_MINUTES.get(window_type or "daily", 36 * 60)


def _market_types_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.endswith("up/down") and right.endswith("up/down"):
        return True
    return {left, right} <= {"price target", "daily close", "weekly close", "monthly close"}


def _target_terms(parsed: ParsedCryptoMarket) -> set[str]:
    if parsed.direction == "between":
        return {str(int(value)) for value in (parsed.lower_bound, parsed.upper_bound) if value is not None and value.is_integer()}
    if parsed.target_price is None:
        return set()
    return {str(int(parsed.target_price)) if parsed.target_price.is_integer() else str(parsed.target_price)}
