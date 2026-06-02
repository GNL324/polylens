from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class NotifierError(RuntimeError):
    pass


class MissingWebhookURLError(NotifierError):
    pass


class ConsoleNotifier:
    def notify(self, payload: dict[str, Any]) -> dict[str, Any]:
        print(format_alert(payload))
        return {"channel": "console", "sent": True}


class WebhookNotifier:
    def __init__(self, url: str | None = None, timeout: int = 15) -> None:
        self.url = url or os.environ.get("POLYLENS_WEBHOOK_URL")
        self.timeout = timeout
        if not self.url:
            raise MissingWebhookURLError("POLYLENS_WEBHOOK_URL is required when --webhook is used")

    def notify(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = _webhook_body(payload, str(self.url))
        data = json.dumps(body).encode("utf-8")
        request = Request(str(self.url), data=data, headers={"Content-Type": "application/json", "User-Agent": "polylens/0.1"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NotifierError(f"webhook notification failed with HTTP {exc.code}: {detail[:200]}") from exc
        except Exception as exc:
            raise NotifierError(f"webhook notification failed: {exc}") from exc
        return {"channel": "webhook", "sent": True, "status": status}


def build_notifier(use_webhook: bool = False):
    if use_webhook:
        return WebhookNotifier()
    return ConsoleNotifier()


def build_alert_payload(candidate: dict[str, Any], timestamp: datetime | None = None) -> dict[str, Any]:
    timestamp = timestamp or datetime.now(timezone.utc)
    pm_title = candidate.get("polymarket_title")
    kalshi_title = candidate.get("kalshi_title")
    sportsbook = candidate.get("sportsbook")
    title = f"Polylens opportunity: {candidate.get('venue_pair') or 'live arbitrage'}"
    return {
        "title": title,
        "venue_pair": candidate.get("venue_pair"),
        "market_names": {
            "polymarket": pm_title,
            "kalshi": kalshi_title,
            "sportsbook": sportsbook,
            "sportsbook_team": candidate.get("sportsbook_team"),
            "sportsbook_opponent": candidate.get("sportsbook_opponent"),
        },
        "edge": candidate.get("estimated_edge"),
        "execution_score": candidate.get("execution_score"),
        "score_reason": candidate.get("score_reason"),
        "prices_odds": {
            "polymarket_yes": candidate.get("polymarket_implied_yes_price"),
            "kalshi_yes": candidate.get("kalshi_implied_yes_price"),
            "kalshi_no": candidate.get("kalshi_implied_no_price"),
            "sportsbook_probability": candidate.get("sportsbook_implied_probability"),
            "odds": candidate.get("odds"),
        },
        "close_time": candidate.get("close_time") or candidate.get("commence_time") or candidate.get("expiration_time") or candidate.get("end_date"),
        "timestamp": timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "candidate": candidate,
    }


def format_alert(payload: dict[str, Any]) -> str:
    names = payload.get("market_names") or {}
    parts = [
        str(payload.get("title") or "Polylens opportunity"),
        f"venue_pair={payload.get('venue_pair')}",
        f"edge={payload.get('edge')}",
        f"execution_score={payload.get('execution_score')}",
        f"markets={names.get('polymarket') or names.get('kalshi')} <> {names.get('kalshi') or names.get('sportsbook') or names.get('sportsbook_team')}",
        f"close_time={payload.get('close_time')}",
        f"reason={payload.get('score_reason')}",
    ]
    return "\n".join(parts)


def _webhook_body(payload: dict[str, Any], url: str) -> dict[str, Any]:
    if "discord" in url.lower():
        return {"content": format_alert(payload), "embeds": [{"title": payload.get("title"), "description": payload.get("score_reason") or "", "fields": [
            {"name": "Venue Pair", "value": str(payload.get("venue_pair")), "inline": True},
            {"name": "Edge", "value": str(payload.get("edge")), "inline": True},
            {"name": "Execution Score", "value": str(payload.get("execution_score")), "inline": True},
        ]}]}
    return payload
