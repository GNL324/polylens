from __future__ import annotations
import os, time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from src.trading.paper import PaperPortfolio
from src.trading.risk import RiskConfig, RiskState, evaluate_order, live_trading_enabled, normalize_order_signal, signal_key
from src.risk import RiskEngine, RiskRequest
from src.risk.models import RiskConfig as PersistentRiskConfig
class KalshiExecutor:
    def __init__(self, config=None, state=None, portfolio=None):
        self.config = config or RiskConfig.from_env()
        self.state = state or RiskState()
        self.risk_db_path = self._risk_db_path_for_portfolio(portfolio)
        self.portfolio = portfolio or PaperPortfolio()
    @staticmethod
    def _risk_db_path_for_portfolio(portfolio):
        if portfolio is None or portfolio.journal_path is None:
            return "data/polylens.db"
        return str(Path(portfolio.journal_path).with_suffix(".risk.db"))
    def submit_order(self, ticker, side, price, count):
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
        stake = signal["price"] * signal["count"]
        key = f"{signal_key(signal)}|dry={self.config.dry_run}|live={self.config.live_trading}"
        cfg = PersistentRiskConfig(max_position_size=self.config.max_trade_dollars, max_exposure_per_venue=self.config.max_open_exposure, max_daily_loss=self.config.max_daily_loss, duplicate_opportunity_cooldown_seconds=0, dry_run=self.config.dry_run, live_trading=self.config.live_trading)
        risk = RiskEngine(db_path=self.risk_db_path, config=cfg).evaluate(RiskRequest(venue="kalshi", market=signal["ticker"], opportunity_id=key, proposed_stake=stake, score=1.0, edge=0.0, dry_run=self.config.dry_run, live_trading=self.config.live_trading, metadata={"source": "kalshi_executor"}))
        if not risk.approved:
            return {"accepted": False, "mode": "rejected", "reason": risk.reason, "risk_decision": risk.to_dict(), "signal": signal}
        order = self.portfolio.place_order(signal["ticker"], signal["side"], signal["price"], signal["count"])
        self.state.open_exposure += stake
        self.state.signal_timestamps[key] = time.time()
        exp = RiskEngine(db_path=self.risk_db_path, config=cfg).record_position("kalshi", signal["ticker"], stake, opportunity_id=key, metadata={"source": "kalshi_executor"})
        return {"accepted": True, "mode": "paper", "order": order.to_dict(), "risk": {"open_exposure": self.state.open_exposure, "persistent_market_exposure": exp}, "risk_decision": risk.to_dict()}
    def _submit_live_order(self, signal):
        return {"accepted": False, "mode": "live_disabled", "reason": "live order placement is not implemented", "signal": signal}
class ShortCryptoExecutor:
    def __init__(self, risk_db_path="data/polylens.db", max_trade_stake=50.0, max_daily_loss=250.0, max_consecutive_losses=5, kill_switch="/home/noel/polylens/.kill_switch"):
        self.risk_db_path = risk_db_path
        self.max_trade_stake = float(max_trade_stake)
        self.max_daily_loss = float(max_daily_loss)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.kill_switch = Path(kill_switch)
    def execute(self, signal, mode="paper"):
        if mode != "paper":
            raise RuntimeError("only paper mode supported in this release")
        if self.kill_switch.exists():
            return {"accepted": False, "mode": "rejected", "reason": "kill switch active"}
        if signal.get("roi", 0) < 0.01:
            return {"accepted": False, "mode": "rejected", "reason": "edge below threshold"}
        stake = min(self.max_trade_stake, max(1.0, float(signal.get("spot_price", 0)) * 0.01))
        return {"accepted": True, "mode": "paper", "order": {"asset": signal["asset"], "direction": signal["direction"], "venue": signal["venue"], "ticker": signal["ticker"], "stake": stake}, "stake": stake}
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
