from __future__ import annotations

from typing import Any


SPORTSBOOK_REGISTRY: dict[str, dict[str, Any]] = {
    "fanduel": {
        "internal_key": "fanduel",
        "display_name": "FanDuel",
        "provider_keys": {"odds_api": ["fanduel"], "oddsblaze": ["FanDuel NY", "FanDuel"]},
        "homepage_url": "https://sportsbook.fanduel.com/",
        "sport_urls": {"basketball_nba": "https://sportsbook.fanduel.com/navigation/nba"},
    },
    "draftkings": {
        "internal_key": "draftkings",
        "display_name": "DraftKings",
        "provider_keys": {"odds_api": ["draftkings"], "oddsblaze": ["DraftKings NY", "DraftKings"]},
        "homepage_url": "https://sportsbook.draftkings.com/",
        "sport_urls": {"basketball_nba": "https://sportsbook.draftkings.com/leagues/basketball/nba"},
    },
    "betmgm": {
        "internal_key": "betmgm",
        "display_name": "BetMGM",
        "provider_keys": {"odds_api": ["betmgm"], "oddsblaze": ["BetMGM NY", "BetMGM"]},
        "homepage_url": "https://www.ny.betmgm.com/en/sports",
        "sport_urls": {"basketball_nba": "https://www.ny.betmgm.com/en/sports/basketball-7"},
    },
    "betrivers": {
        "internal_key": "betrivers",
        "display_name": "BetRivers",
        "provider_keys": {"odds_api": ["betrivers"], "oddsblaze": ["BetRivers NY", "BetRivers"]},
        "homepage_url": "https://ny.betrivers.com/",
        "sport_urls": {"basketball_nba": "https://ny.betrivers.com/?page=sportsbook&group=1000093652&type=matches"},
    },
    "bovada": {
        "internal_key": "bovada",
        "display_name": "Bovada",
        "provider_keys": {"odds_api": ["bovada"], "oddsblaze": ["Bovada"]},
        "homepage_url": "https://www.bovada.lv/sports",
        "sport_urls": {"basketball_nba": "https://www.bovada.lv/sports/basketball/nba"},
    },
    "caesars": {
        "internal_key": "caesars",
        "display_name": "Caesars",
        "provider_keys": {"odds_api": ["caesars"], "oddsblaze": ["Caesars NY", "Caesars"]},
        "homepage_url": "https://sportsbook.caesars.com/",
        "sport_urls": {"basketball_nba": "https://sportsbook.caesars.com/us/ny/bet/basketball"},
    },
    "espnbet": {
        "internal_key": "espnbet",
        "display_name": "ESPN Bet",
        "provider_keys": {"odds_api": ["espnbet"], "oddsblaze": ["ESPN Bet", "ESPNBET"]},
        "homepage_url": "https://espnbet.com/sports",
        "sport_urls": {"basketball_nba": "https://espnbet.com/sports/basketball"},
    },
    "fanatics": {
        "internal_key": "fanatics",
        "display_name": "Fanatics",
        "provider_keys": {"odds_api": ["fanatics"], "oddsblaze": ["Fanatics NY", "Fanatics"]},
        "homepage_url": "https://sportsbook.fanatics.com/",
        "sport_urls": {"basketball_nba": "https://sportsbook.fanatics.com/sports/basketball"},
    },
}


def sportsbook_registry() -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in SPORTSBOOK_REGISTRY.items()}


def sportsbook_catalog() -> list[dict[str, str]]:
    return [{"key": key, "label": row["display_name"]} for key, row in SPORTSBOOK_REGISTRY.items()]


def sportsbook_labels() -> dict[str, str]:
    return {key: row["display_name"] for key, row in SPORTSBOOK_REGISTRY.items()}


def get_sportsbook(key: object) -> dict[str, Any] | None:
    internal_key = normalize_sportsbook_key(key)
    row = SPORTSBOOK_REGISTRY.get(internal_key)
    return dict(row) if row else None


def normalize_sportsbook_key(value: object, *, provider: str | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    folded = _fold(raw)
    if folded in SPORTSBOOK_REGISTRY:
        return folded
    for internal_key, row in SPORTSBOOK_REGISTRY.items():
        if folded == _fold(row["display_name"]):
            return internal_key
        providers = [provider] if provider else row.get("provider_keys", {}).keys()
        for provider_name in providers:
            for provider_key in row.get("provider_keys", {}).get(str(provider_name), []):
                if folded == _fold(provider_key):
                    return internal_key
    return folded


def normalize_provider_sportsbook_key(provider: str, provider_key: object) -> str:
    return normalize_sportsbook_key(provider_key, provider=provider)


def sportsbook_display_name(key: object) -> str:
    internal_key = normalize_sportsbook_key(key)
    return SPORTSBOOK_REGISTRY.get(internal_key, {}).get("display_name", str(key or ""))


def sportsbook_url(key: object, sport: object | None = None) -> str:
    row = get_sportsbook(key)
    if not row:
        return ""
    sport_key = str(sport or "").strip()
    if sport_key:
        url = row.get("sport_urls", {}).get(sport_key)
        if url:
            return str(url)
    return str(row.get("homepage_url") or "")


def sportsbook_link_label(key: object) -> str:
    name = sportsbook_display_name(key)
    return f"{name} ↗" if name else ""


def _fold(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")
