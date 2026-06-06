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
from src.risk import RiskEngine, RiskRequest
from src.risk.models import RiskConfig as PersistentRiskConfig, RiskDecisionType
from src.storage.opportunity_store import OpportunityStore
from src.storage.opportunities import record_alert_event


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
    save: bool = False,
    db_path: str = "data/polylens.db",
    **scan_kwargs: Any,
) -> dict[str, Any]:
    suppressor = suppressor or DuplicateSuppressor()
    iterations = 0
    total_alerts = 0
    last_result: dict[str, Any] = {}
    store = OpportunityStore(db_path) if save else None
    risk_config = PersistentRiskConfig.from_env()
    risk_engine = RiskEngine(db_path, PersistentRiskConfig(**{**risk_config.__dict__, "duplicate_opportunity_cooldown_seconds": 0}))
    while True:
        iterations += 1
        timestamp = datetime.now(timezone.utc)
        result = run_live_scan(**scan_kwargs)
        sent_payloads = []
        suppressed = 0
        risk_rejections = 0
        scan_run_id = None
        opportunity_ids: list[int] = []
        if store:
            scan_run_id, opportunity_ids = store.save_scan_result(result, scan_mode="watch-live-arb", filters=scan_kwargs, alert_count=0)
        for candidate_index, candidate in enumerate(result.get("top_candidates", [])):
            if suppressor.should_alert(candidate, timestamp=timestamp):
                opportunity_id = opportunity_ids[candidate_index] if candidate_index < len(opportunity_ids) else None
                risk_decision = risk_engine.evaluate(_risk_request_from_candidate(candidate, opportunity_id=opportunity_id))
                if risk_decision.decision not in {RiskDecisionType.APPROVE, RiskDecisionType.PAPER_ONLY}:
                    risk_rejections += 1
                    record_alert_event(
                        db_path=db_path,
                        alert_type="live_arbitrage",
                        channel="webhook" if notifier.__class__.__name__ == "WebhookNotifier" else "console",
                        opportunity_id=opportunity_id,
                        market_title=_candidate_title(candidate),
                        roi=candidate.get("estimated_edge") if candidate.get("estimated_edge") is not None else candidate.get("raw_edge"),
                        profit=candidate.get("expected_profit") or candidate.get("guaranteed_profit_amount"),
                        status="skipped",
                        reason=risk_decision.reason,
                        raw={"candidate": candidate, "risk_decision": risk_decision.to_dict()},
                    )
                    continue
                payload = build_alert_payload(candidate, timestamp=timestamp)
                payload["risk_decision"] = risk_decision.to_dict()
                destination = "webhook" if notifier.__class__.__name__ == "WebhookNotifier" else "console"
                status = "sent"
                error = None
                if not (as_json and notifier.__class__.__name__ == "ConsoleNotifier"):
                    try:
                        response = notifier.notify(payload)
                        status = str(response.get("status") or "sent") if isinstance(response, dict) else "sent"
                    except Exception as exc:
                        status = "error"
                        error = str(exc)
                record_alert_event(
                    db_path=db_path,
                    alert_type="live_arbitrage",
                    channel=destination,
                    opportunity_id=opportunity_id,
                    market_title=_candidate_title(candidate),
                    roi=candidate.get("estimated_edge") if candidate.get("estimated_edge") is not None else candidate.get("raw_edge"),
                    profit=candidate.get("expected_profit") or candidate.get("guaranteed_profit_amount"),
                    status="failed" if status == "error" else "sent",
                    reason="webhook failed" if status == "error" else "send success",
                    raw={"candidate": candidate, "payload": payload, "status": status, "error": error},
                )
                sent_payloads.append(payload)
                if store:
                    store.save_alert(opportunity_id=opportunity_id, scan_run_id=scan_run_id, destination=destination, status=status, error=error, payload=payload)
            else:
                suppressed += 1
                record_alert_event(
                    db_path=db_path,
                    alert_type="live_arbitrage",
                    channel="webhook" if notifier.__class__.__name__ == "WebhookNotifier" else "console",
                    market_title=_candidate_title(candidate),
                    roi=candidate.get("estimated_edge") if candidate.get("estimated_edge") is not None else candidate.get("raw_edge"),
                    profit=candidate.get("expected_profit") or candidate.get("guaranteed_profit_amount"),
                    status="duplicate",
                    reason="duplicate suppression",
                    raw={"candidate": candidate},
                )
        total_alerts += len(sent_payloads)
        if store and scan_run_id is not None and sent_payloads:
            store.save_scan_run(scan_mode="watch-live-arb-alert-summary", filters=scan_kwargs, venues_scanned={}, candidate_count=0, alert_count=len(sent_payloads), raw_result={"scan_run_id": scan_run_id})
        last_result = {
            "iterations": iterations,
            "alerts_sent": len(sent_payloads),
            "total_alerts_sent": total_alerts,
            "duplicates_suppressed": suppressed,
            "risk_rejections": risk_rejections,
            "scan": result,
            "alerts": sent_payloads,
        }
        if as_json:
            print(json.dumps(last_result, indent=2, sort_keys=True))
        if once:
            return last_result
        time.sleep(max(1, int(interval_seconds)))


def _risk_request_from_candidate(candidate: dict[str, Any], opportunity_id: int | None = None) -> RiskRequest:
    venue_pair = str(candidate.get("venue_pair") or "unknown")
    venue = venue_pair.split("_", 1)[0] if "_" in venue_pair else venue_pair
    market = (
        candidate.get("kalshi_ticker")
        or candidate.get("polymarket_id")
        or candidate.get("sportsbook_event_id")
        or candidate.get("sportsbook_team")
        or "unknown"
    )
    key = str(opportunity_id or candidate.get("opportunity_id") or _candidate_key(candidate))
    return RiskRequest(
        venue=venue,
        market=str(market),
        opportunity_id=key,
        proposed_stake=float(candidate.get("proposed_stake") or candidate.get("recommended_stake") or 0.0),
        score=candidate.get("execution_score"),
        edge=candidate.get("estimated_edge") if candidate.get("estimated_edge") is not None else candidate.get("raw_edge"),
        metadata={"source": "watch_live_arbitrage", "venue_pair": venue_pair},
    )


def _candidate_key(candidate: dict[str, Any]) -> str:
    return "|".join(
        str(candidate.get(field) or "")
        for field in ("venue_pair", "polymarket_id", "kalshi_ticker", "sportsbook_event_id", "sportsbook_team")
    )


def _candidate_title(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("market_title")
        or candidate.get("polymarket_title")
        or candidate.get("kalshi_title")
        or candidate.get("sportsbook_event_name")
        or candidate.get("venue_pair")
        or "live arbitrage"
    )
