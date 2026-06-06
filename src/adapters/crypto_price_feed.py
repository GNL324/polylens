from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)


class CryptoPriceFeedError(RuntimeError):
    pass


@dataclass
class PriceTick:
    source: str
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    last: Optional[float] = None
    timestamp: Optional[float] = None
    latency_ms: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_stale(self, max_age: float = 2.0) -> bool:
        return self.timestamp is None or (time.time() - self.timestamp) > max_age


class BaseCryptoFeed:
    def __init__(self, symbol: str = "BTC-USD") -> None:
        self.symbol = symbol.upper()
        self._latest: Optional[PriceTick] = None
        self._lock = threading.Lock()

    def latest(self) -> PriceTick:
        with self._lock:
            return self._latest

    def _set_latest(self, tick: PriceTick) -> None:
        with self._lock:
            self._latest = tick

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class CoinbaseFeed(BaseCryptoFeed):
    def start(self) -> None:
        LOGGER.info("starting Coinbase feed symbol=%s", self.symbol)
        try:
            import websocket  # type: ignore
        except ModuleNotFoundError as exc:
            raise CryptoPriceFeedError("Coinbase feed requires 'websocket-client'; install websocket-client") from exc
        url = "wss://ws-feed.exchange.coinbase.com"
        self._stop = threading.Event()
        def on_open(ws: Any) -> None:
            LOGGER.info("coinbase ws open")
            ws.send(
                json.dumps(
                    {"type": "subscribe", "channels": [{"name": "ticker", "product_ids": [self.symbol]}]}
                )
            )

        def on_message(ws: Any, message: str) -> None:
            try:
                data = json.loads(message)
                if data.get("type") != "ticker":
                    return
                best_bid = float(data.get("best_bid", 0.0) or 0.0)
                best_ask = float(data.get("best_ask", 0.0) or 0.0)
                last = float(data.get("price", 0.0) or 0.0)
                now = time.time()
                tick = PriceTick(
                    source="coinbase",
                    symbol=self.symbol,
                    bid=best_bid,
                    ask=best_ask,
                    mid=(best_bid + best_ask) / 2 if best_bid and best_ask else None,
                    last=last,
                    timestamp=now,
                    latency_ms=data.get("latency"),
                    raw=data,
                )
                self._set_latest(tick)
            except Exception:
                LOGGER.debug("coinbase ticker parse error", exc_info=True)

        def on_error(ws: Any, error: Exception) -> None:
            LOGGER.warning("coinbase ws error: %s", error)

        def on_close(ws: Any, status: int, msg: str) -> None:
            LOGGER.info("coinbase ws closed status=%s msg=%s", status, msg)

        def run() -> None:
            while not self._stop.is_set():
                try:
                    ws = websocket.WebSocketApp(
                        url,
                        on_open=on_open,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close,
                    )
                    ws.run_forever(ping_interval=20, ping_timeout=20)
                except Exception:
                    LOGGER.debug("coinbase ws run_forever error", exc_info=True)
                    time.sleep(1)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if hasattr(self, "_thread"):
            self._thread.join(timeout=3)


class BinanceFeed(BaseCryptoFeed):
    def _normalize(self, symbol: str) -> str:
        return symbol.replace("-", "").lower()

    def start(self) -> None:
        LOGGER.info("starting Binance feed symbol=%s", self.symbol)
        try:
            import websocket  # type: ignore
        except ModuleNotFoundError as exc:
            raise CryptoPriceFeedError("Binance feed requires 'websocket-client'; install websocket-client") from exc
        s = self._normalize(self.symbol)
        url = f"wss://stream.binance.com:9443/ws/{s}@ticker"
        self._stop = threading.Event()

        def on_open(ws: Any) -> None:
            LOGGER.info("binance ws open")

        def on_message(ws: Any, message: str) -> None:
            try:
                data = json.loads(message)
                last = float(data.get("c", 0.0) or 0.0)
                bid = float(data.get("b", [0.0])[0] if data.get("b") else 0.0)
                ask = float(data.get("a", [0.0])[0] if data.get("a") else 0.0)
                now = time.time()
                tick = PriceTick(
                    source="binance",
                    symbol=self.symbol,
                    bid=bid if bid else None,
                    ask=ask if ask else None,
                    mid=(bid + ask) / 2 if bid and ask else None,
                    last=last,
                    timestamp=now,
                    latency_ms=None,
                    raw=data,
                )
                self._set_latest(tick)
            except Exception:
                LOGGER.debug("binance ticker parse error", exc_info=True)

        def on_error(ws: Any, error: Exception) -> None:
            LOGGER.warning("binance ws error: %s", error)

        def on_close(ws: Any, status: int, msg: str) -> None:
            LOGGER.info("binance ws closed status=%s msg=%s", status, msg)

        def run() -> None:
            while not self._stop.is_set():
                try:
                    ws = websocket.WebSocketApp(
                        url,
                        on_open=on_open,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close,
                    )
                    ws.run_forever(ping_interval=20, ping_timeout=20)
                except Exception:
                    LOGGER.debug("binance ws run_forever error", exc_info=True)
                    time.sleep(1)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if hasattr(self, "_thread"):
            self._thread.join(timeout=3)


class CryptoPriceFeedManager:
    def __init__(self, symbols: Optional[List[str]] = None) -> None:
        default_symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
        self.symbols = [s.upper() for s in (symbols or default_symbols)]
        self._feeds: Dict[str, BaseCryptoFeed] = {}
        self._started = False
        self.staleness_limit = float(os.environ.get("POLYLENS_CRYPTO_STALENESS_SECS", "2.0"))

    def _feed_for_symbol(self, symbol: str) -> BaseCryptoFeed:
        if symbol not in self._feeds:
            exchange = os.environ.get("POLYLENS_CRYPTO_FEED", "coinbase").lower()
            if exchange in {"coinbase", "cb"}:
                self._feeds[symbol] = CoinbaseFeed(symbol)
            else:
                self._feeds[symbol] = BinanceFeed(symbol)
            LOGGER.info("crypto feed selected symbol=%s exchange=%s", symbol, exchange)
        return self._feeds[symbol]

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for symbol in self.symbols:
            try:
                self._feed_for_symbol(symbol).start()
            except Exception as exc:
                LOGGER.warning("failed to start crypto feed symbol=%s error=%s", symbol, exc)

    def stop(self) -> None:
        for symbol, feed in list(self._feeds.items()):
            try:
                feed.stop()
            except Exception:
                pass

    def get_latest(self, symbol: str) -> Optional[PriceTick]:
        feed = self._feeds.get(symbol.upper())
        if not feed:
            return None
        tick = feed.latest()
        if not tick:
            return None
        if tick.is_stale(self.staleness_limit):
            LOGGER.warning("crypto feed stale symbol=%s age=%.2fs", symbol, time.time() - (tick.timestamp or 0))
            return None
        return tick
