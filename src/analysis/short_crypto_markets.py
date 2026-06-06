from __future__ import annotations
import json, logging, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShortCryptoMarket:
    asset: str
    venue: str
    ticker: str
    start_ts: Optional[float]
    end_ts: Optional[float]
    direction: str
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    no_bid: Optional[float]
    no_ask: Optional[float]
    liquidity: Optional[float]
    strike_price: Optional[float] = None
    reference_price: Optional[float] = None
    timestamp: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_valid(self, max_age: float = 2.0, *, now: Optional[float] = None) -> bool:
        current = now if now is not None else time.time()
        if self.asset not in {"BTC", "ETH", "SOL"}:
            return False
        if self.direction not in {"up", "down"}:
            return False
        if self.yes_bid is None or self.yes_ask is None:
            return False
        if self.timestamp is None:
            return False
        if self.timestamp > current + 5.0:
            return False
        if (current - self.timestamp) > max_age:
            return False
        return True

    def implied_prob(self, side: str = "yes"):
        if side == "yes" and self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        if side == "no" and self.no_bid is not None and self.no_ask is not None:
            return (self.no_bid + self.no_ask) / 2.0
        return None

    def mid_spread(self, side: str = "yes"):
        p = self.implied_prob(side)
        if p is None:
            return None
        if side == "yes" and self.yes_bid is not None and self.yes_ask is not None:
            return self.yes_ask - self.yes_bid
        if side == "no" and self.no_bid is not None and self.no_ask is not None:
            return self.no_ask - self.no_bid
        return None


def normalize_short_crypto_markets(kalshi_markets, polymarket_markets):
    now = time.time()
    out = []
    out.extend(_parse_kalshi(kalshi_markets or [], now=now))
    out.extend(_parse_polymarket(polymarket_markets or [], now=now))
    return out


def _parse_kalshi(markets, now=None):
    res = []
    for m in markets:
        title = (m.get("title") or m.get("question") or "").upper()
        asset = next((a for a in ("BTC", "ETH", "SOL") if title.startswith(a)), None)
        if asset is None:
            continue
        direction = "up" if "-UP" in title else ("down" if "-DOWN" in title else None)
        if direction is None:
            continue
        p = m.get("pricing", {})
        yes_bid = _f(p.get("yes_bid") or m.get("yes_bid"))
        yes_ask = _f(p.get("yes_ask") or m.get("yes_ask"))
        no_bid = _f(p.get("no_bid") or m.get("no_bid"))
        no_ask = _f(p.get("no_ask") or m.get("no_ask"))
        res.append(
            ShortCryptoMarket(
                asset=asset,
                venue="kalshi",
                ticker=m.get("ticker") or m.get("market_identity", {}).get("ticker") or "",
                start_ts=_ts(m.get("market_identity", {}).get("open_time") or m.get("open_time")),
                end_ts=_ts(m.get("market_identity", {}).get("close_time") or m.get("close_time")),
                direction=direction,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                liquidity=_f(p.get("liquidity") or m.get("liquidity")),
                timestamp=now,
                raw=m,
            )
        )
    return res


def _parse_polymarket(markets, now=None):
    res = []
    for m in markets:
        title = (m.get("title") or m.get("question") or "").upper()
        asset = next((a for a in ("BTC", "ETH", "SOL") if title.startswith(a)), None)
        if asset is None:
            continue
        direction = "up" if "-UP" in title else ("down" if "-DOWN" in title else None)
        if direction is None:
            continue
        prices = m.get("outcome_prices") or []
        yes = _f(prices[0]) if len(prices) > 0 else None
        no = _f(prices[1]) if len(prices) > 1 else None
        res.append(
            ShortCryptoMarket(
                asset=asset,
                venue="polymarket",
                ticker=m.get("condition_id") or m.get("market_id") or m.get("id") or "",
                start_ts=None,
                end_ts=_ts(m.get("end_date") or m.get("endDate")),
                direction=direction,
                yes_bid=yes,
                yes_ask=yes,
                no_bid=no,
                no_ask=no,
                liquidity=_f(m.get("liquidity") or m.get("liquidityNum")),
                timestamp=now,
                raw=m,
            )
        )
    return res


def _f(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return None
