from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.adapters.kalshi import KalshiClient
from src.adapters.odds_api import MissingOddsAPIKey, OddsAPIClient
from src.adapters.polymarket import PolymarketClient
from src.alerts.notifier import build_alert_payload
from src.analysis.live_arbitrage import scan_live_arbitrage
from src.analysis.odds_normalization import normalize_odds_events


@dataclass
class DuplicateSuppressor:
    bucket_seconds: int = 900
    seen: set[tuple[Any, ...]] = field(default_factory=set)

    def should_alert(self, candidate: dict[str, Any], timestamp: datetime | None = None) -> bool:
        timestamp = timestamp or datetime.now(timezone.utc)
        key = self._key(candidate, timestamp)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def _key(self, candidate: dict[str, Any], timestamp: datetime) -> tuple[Any, ...]:
        bucket = int(timestamp.timestamp() // self.bucket_seconds)
        return (
            candidate.get("venue_pair"),
            candidate.get("polymarket_id"),
            candidate.get("kalshi_ticker"),
            candidate.get("sportsbook_event_id"),
            candidate.get("sportsbook_team"),
            round(float(candidate.get("estimated_edge") or 0), 4),
            round(float(candidate.get("execution_score") or 0), 4),
            bucket,
        )


def run_live_scan(
    sport_key: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    bookmaker: str | None = None,
    region: str = "us",
    limit: int = 100,
    min_edge: float | None = None,
    min_score: float | None = None,
    max_close_hours: float | None = None,
    include_low_confidence: bool = False,
) -> dict[str, Any]:
    venue_errors: dict[str, str] = {}
    poly_client = PolymarketClient(raw_dir="data/raw")
    kalshi_client = KalshiClient(raw_dir="data/raw")
    try:
        polymarket_markets = poly_client.get_active_markets(keyword=keyword, category=category, sport=sport_key, limit=limit)
    except Exception as exc:
        polymarket_markets = []
        venue_errors["polymarket"] = f"Polymarket live discovery failed: {exc}"
    try:
        kalshi_markets = kalshi_client.get_markets(status="open", limit=min(max(limit, 1), 1000), max_pages=5)
    except Exception as exc:
        kalshi_markets = []
        venue_errors["kalshi"] = f"Kalshi live discovery failed: {exc}"

    sportsbook_lines: list[dict[str, Any]] = []
    sportsbook_skipped_reason: str | None = None
    if sport_key:
        try:
            odds_client = OddsAPIClient(raw_dir="data/raw")
            events = odds_client.get_odds(sport_key, regions=region, markets="h2h,spreads,totals,outrights", bookmakers=bookmaker)
            sportsbook_lines = normalize_odds_events(events)
        except MissingOddsAPIKey:
            sportsbook_skipped_reason = "ODDS_API_KEY missing; sportsbook side skipped"
        except Exception as exc:
            sportsbook_skipped_reason = f"sportsbook odds fetch failed: {exc}"
    else:
        sportsbook_skipped_reason = "--sport not provided; sportsbook side skipped"

    return scan_live_arbitrage(
        polymarket_markets,
        kalshi_markets,
        sportsbook_lines=sportsbook_lines,
        sportsbook_skipped_reason=sportsbook_skipped_reason,
        venue_errors=venue_errors,
        min_edge=min_edge,
        min_score=min_score,
        max_close_hours=max_close_hours,
        include_low_confidence=include_low_confidence,
    )


def watch_live_arbitrage(
    notifier: Any,
    interval_seconds: int = 60,
    once: bool = False,
    as_json: bool = False,
    suppressor: DuplicateSuppressor | None = None,
    **scan_kwargs: Any,
) -> dict[str, Any]:
    suppressor = suppressor or DuplicateSuppressor()
    iterations = 0
    total_alerts = 0
    last_result: dict[str, Any] = {}
    while True:
        iterations += 1
        timestamp = datetime.now(timezone.utc)
        result = run_live_scan(**scan_kwargs)
        sent_payloads = []
        suppressed = 0
        for candidate in result.get("top_candidates", []):
            if suppressor.should_alert(candidate, timestamp=timestamp):
                payload = build_alert_payload(candidate, timestamp=timestamp)
                if not (as_json and notifier.__class__.__name__ == "ConsoleNotifier"):
                    notifier.notify(payload)
                sent_payloads.append(payload)
            else:
                suppressed += 1
        total_alerts += len(sent_payloads)
        last_result = {
            "iterations": iterations,
            "alerts_sent": len(sent_payloads),
            "total_alerts_sent": total_alerts,
            "duplicates_suppressed": suppressed,
            "scan": result,
            "alerts": sent_payloads,
        }
        if as_json:
            print(json.dumps(last_result, indent=2, sort_keys=True))
        if once:
            return last_result
        time.sleep(max(1, int(interval_seconds)))
