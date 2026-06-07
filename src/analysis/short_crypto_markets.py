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
    window_minutes: Optional[int] = None
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
        if self.yes_bid < 0 or self.yes_ask <= 0 or self.yes_bid > self.yes_ask:
            return False
        if self.timestamp is None:
            return False
        if self.timestamp > current + 5.0:
            return False
        if (current - self.timestamp) > max_age:
            return False
        if self.liquidity is not None and self.liquidity <= 0:
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
        ticker = (m.get("ticker") or m.get("market_identity", {}).get("ticker") or "").upper()
        asset = _kalshi_asset(title, ticker)
        if asset is None:
            continue
        direction = "up" if "-UP" in title or "-T" in ticker else ("down" if "-DOWN" in title or "-B" in ticker else None)
        if direction is None:
            continue
        p = m.get("pricing", {})
        yes_bid = _f(_first_present(p.get("yes_bid"), m.get("yes_bid")))
        yes_ask = _f(_first_present(p.get("yes_ask"), m.get("yes_ask")))
        no_bid = _f(_first_present(p.get("no_bid"), m.get("no_bid")))
        no_ask = _f(_first_present(p.get("no_ask"), m.get("no_ask")))
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
                liquidity=_f(_first_present(p.get("liquidity"), m.get("liquidity"))),
                window_minutes=_window_minutes(m),
                strike_price=_strike_from_ticker(ticker),
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
        book = _polymarket_clob_book(m)
        if not book:
            LOGGER.warning("skipping Polymarket short crypto market without CLOB/orderbook data ticker=%s", m.get("condition_id") or m.get("id"))
            continue
        res.append(
            ShortCryptoMarket(
                asset=asset,
                venue="polymarket",
                ticker=m.get("condition_id") or m.get("market_id") or m.get("id") or "",
                start_ts=None,
                end_ts=_ts(m.get("end_date") or m.get("endDate")),
                direction=direction,
                yes_bid=book.get("yes_bid"),
                yes_ask=book.get("yes_ask"),
                no_bid=book.get("no_bid"),
                no_ask=book.get("no_ask"),
                liquidity=book.get("liquidity"),
                window_minutes=_window_minutes(m),
                timestamp=book.get("timestamp") or now,
                raw=m,
            )
        )
    return res


def _f(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _first_present(*values):
    for value in values:
        if value is not None and value != "":
            return value
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


def _window_minutes(m):
    for key in ("window_minutes", "window", "duration_minutes"):
        value = m.get(key)
        if value is not None:
            try:
                return int(value)
            except Exception:
                pass
    title = (m.get("title") or m.get("question") or "").upper()
    for marker in ("-5", " 5 ", "5M", "5 MIN"):
        if marker in title:
            return 5
    for marker in ("-10", " 10 ", "10M", "10 MIN"):
        if marker in title:
            return 10
    for marker in ("-15", " 15 ", "15M", "15 MIN"):
        if marker in title:
            return 15
    return None


def _kalshi_asset(title, ticker):
    text = f"{ticker} {title}"
    for asset in ("BTC", "ETH", "SOL"):
        if ticker.startswith(f"{asset}-") or title.startswith(f"{asset}-"):
            return asset
    if "KXBT" in ticker or " BITCOIN " in f" {title} " or title.startswith("BITCOIN"):
        return "BTC"
    if "KXETH" in ticker or " ETHEREUM " in f" {title} " or title.startswith("ETHEREUM"):
        return "ETH"
    if "KXSOL" in ticker or " SOLANA " in f" {title} " or title.startswith("SOL PRICE") or title.startswith("SOL "):
        return "SOL"
    if " BTC " in f" {text} ":
        return "BTC"
    if " ETH " in f" {text} ":
        return "ETH"
    if " SOL " in f" {text} ":
        return "SOL"
    return None


def _strike_from_ticker(ticker):
    if "-T" not in ticker and "-B" not in ticker:
        return None
    marker = "-T" if "-T" in ticker else "-B"
    raw = ticker.rsplit(marker, 1)[-1]
    return _f(raw)


def _polymarket_clob_book(m):
    book = m.get("clob_orderbook") or m.get("orderbook") or m.get("book") or {}
    if not isinstance(book, dict):
        return {}
    yes = book.get("yes") or book.get("YES") or book
    no = book.get("no") or book.get("NO") or {}
    yes_bid = _best_price(yes, "bids", best=max)
    yes_ask = _best_price(yes, "asks", best=min)
    no_bid = _best_price(no, "bids", best=max)
    no_ask = _best_price(no, "asks", best=min)
    liquidity = _f(book.get("liquidity") or m.get("liquidity") or m.get("liquidityNum"))
    if liquidity is None:
        liquidity = _book_depth(yes, "bids") + _book_depth(yes, "asks")
    ts = _f(book.get("timestamp") or book.get("ts") or m.get("book_timestamp") or m.get("orderbook_timestamp"))
    if yes_bid is None or yes_ask is None:
        return {}
    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask, "liquidity": liquidity, "timestamp": ts}


def _best_price(container, side, best):
    levels = container.get(side) if isinstance(container, dict) else None
    if not isinstance(levels, list):
        return None
    prices = []
    for level in levels:
        if isinstance(level, dict):
            price = _f(level.get("price") or level.get("p"))
        elif isinstance(level, (list, tuple)) and level:
            price = _f(level[0])
        else:
            price = None
        if price is not None:
            prices.append(price)
    return best(prices) if prices else None


def _book_depth(container, side):
    levels = container.get(side) if isinstance(container, dict) else None
    if not isinstance(levels, list):
        return 0.0
    depth = 0.0
    for level in levels:
        if isinstance(level, dict):
            size = _f(level.get("size") or level.get("quantity") or level.get("q"))
        elif isinstance(level, (list, tuple)) and len(level) > 1:
            size = _f(level[1])
        else:
            size = None
        depth += size or 0.0
    return depth
