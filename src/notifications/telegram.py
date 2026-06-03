from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_SUPPRESSION: dict[str, float] = {}


def format_prop_alert(opportunity: dict[str, Any], bankroll: float | None = None) -> str:
    profit = opportunity.get("guaranteed_profit_amount")
    roi = opportunity.get("guaranteed_roi") or 0
    stake_text = f" on ${bankroll:.0f}" if bankroll else ""
    lines = [
        "PROP ARBITRAGE",
        "",
        f"Player: {opportunity.get('player')}",
        f"Market: {opportunity.get('prop_type')}",
        f"Line: {opportunity.get('line')}",
        "",
        "Over:",
        f"{opportunity.get('over_book')} {opportunity.get('over_odds')}",
        "",
        "Under:",
        f"{opportunity.get('under_book')} {opportunity.get('under_odds')}",
        "",
        f"ROI: {roi * 100:.2f}%",
        f"Profit: ${profit:.2f}{stake_text}" if profit is not None else "Profit: n/a",
    ]
    return "\n".join(lines)


def should_alert(key: str, window_seconds: int = 900, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    last = _SUPPRESSION.get(key)
    if last is not None and now - last < window_seconds:
        return False
    _SUPPRESSION[key] = now
    return True


def send_telegram_alert(opportunity: dict[str, Any], key: str, bankroll: float | None = None) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "status": "skipped", "error": "telegram env missing"}
    if not should_alert(key):
        return {"sent": False, "status": "duplicate_suppressed", "error": None}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode({"chat_id": chat_id, "text": format_prop_alert(opportunity, bankroll=bankroll)}).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
        return {"sent": True, "status": "sent", "response": json.loads(payload) if payload else None}
    except Exception as exc:
        return {"sent": False, "status": "error", "error": str(exc)}
