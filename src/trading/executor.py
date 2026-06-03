from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.trading.paper import PaperPortfolio
from src.trading.risk import RiskConfig, RiskState, evaluate_order, live_trading_enabled, normalize_order_signal, signal_key


class KalshiExecutor:
    def __init__(self, config: RiskConfig | None = None, state: RiskState | None = None, portfolio: PaperPortfolio | None = None) -> None:
        self.config = config or RiskConfig.from_env()
        self.state = state or RiskState()
        self.portfolio = portfolio or PaperPortfolio()

    def submit_order(self, ticker: str, side: str, price: float, count: int) -> dict[str, Any]:
        raw_signal = {"ticker": ticker, "side": side, "price": price, "count": count}
        try:
            signal = normalize_order_signal(raw_signal)
        except ValueError as exc:
            return {"accepted": False, "mode": "rejected", "reason": str(exc), "signal": raw_signal}
        decision = evaluate_order(signal, self.config, self.state)
        if not decision.accepted:
            return {"accepted": False, "mode": "rejected", "reason": decision.reason, "signal": signal}
        if live_trading_enabled(self.config):
            return self._submit_live_order(signal)
        order = self.portfolio.place_order(signal["ticker"], signal["side"], signal["price"], signal["count"])
        self.state.open_exposure += signal["price"] * signal["count"]
        self.state.signal_timestamps[signal_key(signal)] = time.time()
        result = {"accepted": True, "mode": "paper", "order": order.to_dict(), "risk": {"open_exposure": self.state.open_exposure}}
        _telegram_notify(f"Kalshi paper order: {signal['ticker']} {signal['side']} {signal['count']} @ {signal['price']}")
        return result

    def _submit_live_order(self, signal: dict[str, Any]) -> dict[str, Any]:
        # Intentional safety gate: real Kalshi order placement is not implemented in this release.
        return {"accepted": False, "mode": "live_disabled", "reason": "live order placement is not implemented", "signal": signal}


def _telegram_notify(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        body = urlencode({"chat_id": chat_id, "text": message}).encode()
        request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(request, timeout=5) as response:
            response.read()
    except Exception:
        return
