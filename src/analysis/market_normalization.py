from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

STOPWORDS = {
    "a", "an", "and", "are", "at", "be", "by", "for", "from", "how", "in", "is", "it", "of", "on", "or", "the", "to", "vs", "will", "win", "with",
    "yes", "no", "market", "polymarket", "kalshi", "et", "utc", "am", "pm", "up", "down", "above", "below", "before", "after",
}
SPORT_LEAGUES = {"nba", "nfl", "mlb", "nhl", "ufc", "wnba", "epl", "mls", "ncaa", "fifa"}
SPORT_TERMS = SPORT_LEAGUES | {"soccer", "football", "baseball", "basketball", "hockey", "tennis", "golf", "fight", "finals", "playoffs", "championship"}
CRYPTO_TERMS = {"bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto", "coinbase", "binance"}
POLITICS_TERMS = {"election", "president", "senate", "congress", "trump", "biden", "democrat", "republican", "governor", "mayor"}
ECON_TERMS = {"fed", "inflation", "cpi", "rates", "rate", "jobs", "unemployment", "gdp", "recession"}


@dataclass(frozen=True)
class NormalizedMarketText:
    source: str
    source_id: str
    title: str
    normalized_title: str
    tokens: set[str]
    entities: set[str]
    category_guess: str
    league_guess: str | None
    raw: dict[str, Any]


def normalize_text(text: str) -> str:
    lowered = text.lower().replace("&", " and ")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def tokenize(text: str) -> set[str]:
    normalized = normalize_text(text)
    tokens = {token for token in normalized.split() if len(token) > 2 and token not in STOPWORDS}
    return {canonical_token(token) for token in tokens}


def canonical_token(token: str) -> str:
    aliases = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "nyc": "newyork", "usa": "us"}
    return aliases.get(token, token)


def extract_entities(text: str) -> set[str]:
    entities = set()
    for match in re.findall(r"\b[A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,3}\b", text):
        cleaned = normalize_text(match).replace(" ", "")
        if len(cleaned) > 2 and cleaned not in STOPWORDS:
            entities.add(cleaned)
    return entities


def guess_category(tokens: set[str]) -> str:
    if tokens & SPORT_TERMS:
        return "Sports"
    if tokens & CRYPTO_TERMS:
        return "Crypto"
    if tokens & POLITICS_TERMS:
        return "Politics"
    if tokens & ECON_TERMS:
        return "Economics"
    return "Other"


def guess_league(tokens: set[str]) -> str | None:
    matches = sorted(tokens & SPORT_LEAGUES)
    return matches[0].upper() if matches else None


def normalize_polymarket_market(trade_or_market: dict[str, Any]) -> NormalizedMarketText:
    title = str(trade_or_market.get("title") or trade_or_market.get("slug") or trade_or_market.get("conditionId") or "")
    text = " ".join(str(trade_or_market.get(key) or "") for key in ("title", "slug", "eventSlug", "outcome"))
    tokens = tokenize(text)
    return NormalizedMarketText(
        source="polymarket",
        source_id=str(trade_or_market.get("conditionId") or trade_or_market.get("slug") or title),
        title=title,
        normalized_title=normalize_text(title),
        tokens=tokens,
        entities=extract_entities(text),
        category_guess=guess_category(tokens),
        league_guess=guess_league(tokens),
        raw=trade_or_market,
    )


def normalize_kalshi_market(market: dict[str, Any]) -> NormalizedMarketText:
    title = str(market.get("title") or market.get("ticker") or "")
    text = " ".join(str(market.get(key) or "") for key in ("title", "subtitle", "rules_primary", "rules_secondary", "category", "event_ticker", "series_ticker"))
    tokens = tokenize(text)
    return NormalizedMarketText(
        source="kalshi",
        source_id=str(market.get("ticker") or market.get("market_ticker") or title),
        title=title,
        normalized_title=normalize_text(title),
        tokens=tokens,
        entities=extract_entities(text),
        category_guess=guess_category(tokens),
        league_guess=guess_league(tokens),
        raw=market,
    )


def text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()
