from __future__ import annotations

import json
import math
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

from src.analysis.short_crypto_paper import DEFAULT_DB_PATH, ShortCryptoPaperStore, _load_json, _parse_ts
from src.sqlite_utils import closing_connection

DEFAULT_MIN_SEGMENT_TRADES = 20
PROBABILITY_BUCKET_WIDTH = 0.05
EDGE_BUCKET_WIDTH = 0.02
PRICE_BUCKET_WIDTH = 0.05

FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_features (
    trade_id INTEGER PRIMARY KEY,
    venue TEXT,
    asset TEXT,
    slug TEXT,
    direction TEXT,
    entry_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    entry_price REAL,
    bid REAL,
    ask REAL,
    spread REAL,
    implied_probability REAL,
    model_probability REAL,
    edge REAL,
    outcome TEXT,
    pnl REAL,
    roi REAL,
    hour_of_day_utc INTEGER,
    minute_bucket INTEGER,
    day_of_week INTEGER,
    window_minutes INTEGER,
    seconds_to_expiry_at_entry REAL,
    btc_price_at_entry REAL,
    btc_price_1m_before REAL,
    btc_price_5m_before REAL,
    btc_price_15m_before REAL,
    btc_price_1h_before REAL,
    btc_return_1m REAL,
    btc_return_5m REAL,
    btc_return_15m REAL,
    btc_return_1h REAL,
    btc_volatility_5m REAL,
    feature_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    FOREIGN KEY (trade_id) REFERENCES paper_trades(id)
);
"""


class ShortCryptoDiagnosticsStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.paper_store = ShortCryptoPaperStore(db_path)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.paper_store.connect() as conn:
            conn.executescript(FEATURES_DDL)
            self._migrate_feature_columns(conn)

    def _migrate_feature_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_trade_features)").fetchall()}
        additions = {
            "btc_price_15m_before": "REAL",
            "btc_price_1h_before": "REAL",
            "btc_return_15m": "REAL",
            "btc_return_1h": "REAL",
        }
        for column, definition in additions.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE paper_trade_features ADD COLUMN {column} {definition}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self.paper_store.connect() as conn:
            yield conn

    def sync_features(self, *, refresh: bool = False) -> dict[str, Any]:
        closed = _closed_trades_with_settlements(self.paper_store)
        existing = self._existing_trade_ids(refresh=refresh)
        inserted = 0
        updated = 0
        for trade, settlement in closed:
            trade_id = int(trade["id"])
            if trade_id in existing and not refresh:
                continue
            feature = extract_trade_features(trade, settlement)
            if trade_id in existing:
                self._upsert_feature(feature)
                updated += 1
            else:
                self._upsert_feature(feature)
                inserted += 1
        return {
            "closed_trades": len(closed),
            "features_inserted": inserted,
            "features_updated": updated,
            "features_total": self.feature_count(),
        }

    def feature_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM paper_trade_features").fetchone()[0])

    def load_features(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM paper_trade_features ORDER BY entry_time, trade_id").fetchall()
        return [dict(row) for row in rows]

    def _existing_trade_ids(self, *, refresh: bool) -> set[int]:
        if refresh:
            return set()
        with self.connect() as conn:
            return {int(row[0]) for row in conn.execute("SELECT trade_id FROM paper_trade_features").fetchall()}

    def _upsert_feature(self, feature: dict[str, Any]) -> None:
        columns = [
            "trade_id",
            "venue",
            "asset",
            "slug",
            "direction",
            "entry_time",
            "end_time",
            "entry_price",
            "bid",
            "ask",
            "spread",
            "implied_probability",
            "model_probability",
            "edge",
            "outcome",
            "pnl",
            "roi",
            "hour_of_day_utc",
            "minute_bucket",
            "day_of_week",
            "window_minutes",
            "seconds_to_expiry_at_entry",
            "btc_price_at_entry",
            "btc_price_1m_before",
            "btc_price_5m_before",
            "btc_price_15m_before",
            "btc_price_1h_before",
            "btc_return_1m",
            "btc_return_5m",
            "btc_return_15m",
            "btc_return_1h",
            "btc_volatility_5m",
            "feature_json",
            "computed_at",
        ]
        values = [feature.get(col) for col in columns]
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{col}=excluded.{col}" for col in columns if col != "trade_id")
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO paper_trade_features ({",".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(trade_id) DO UPDATE SET {updates}
                """,
                values,
            )


def extract_trade_features(trade: dict[str, Any], settlement: dict[str, Any]) -> dict[str, Any]:
    raw = _load_json(trade.get("raw_json"))
    entry_ts = _parse_ts(trade["entry_time"])
    end_ts = _parse_ts(trade["end_time"])
    entry_dt = datetime.fromtimestamp(entry_ts, timezone.utc)
    bid, ask = _extract_bid_ask(raw, trade)
    spread = (ask - bid) if bid is not None and ask is not None else None
    outcome = str(settlement.get("result") or trade.get("status") or "")
    btc_context = _btc_context_at_entry(entry_ts, raw.get("spot_price"))
    feature = {
        "trade_id": int(trade["id"]),
        "venue": trade.get("venue"),
        "asset": trade.get("asset"),
        "slug": trade.get("slug"),
        "direction": trade.get("direction"),
        "entry_time": trade.get("entry_time"),
        "end_time": trade.get("end_time"),
        "entry_price": _float_or_none(trade.get("entry_price")),
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "implied_probability": _float_or_none(trade.get("implied_probability")),
        "model_probability": _float_or_none(trade.get("model_probability")),
        "edge": _float_or_none(trade.get("edge")),
        "outcome": outcome,
        "pnl": _float_or_none(settlement.get("pnl")),
        "roi": _float_or_none(settlement.get("roi")),
        "hour_of_day_utc": entry_dt.hour,
        "minute_bucket": (entry_dt.minute // 5) * 5,
        "day_of_week": entry_dt.weekday(),
        "window_minutes": _int_or_none(trade.get("window_minutes")),
        "seconds_to_expiry_at_entry": max(0.0, end_ts - entry_ts),
        "computed_at": _iso_now(),
        **btc_context,
    }
    feature["feature_json"] = json.dumps(feature, sort_keys=True, default=str)
    return feature


def diagnostics_report(
    db_path: str = DEFAULT_DB_PATH,
    *,
    refresh_features: bool = True,
) -> dict[str, Any]:
    store = ShortCryptoDiagnosticsStore(db_path)
    if refresh_features:
        store.sync_features()
    features = store.load_features()
    closed = [item for item in features if item.get("outcome") in {"won", "lost"}]
    metrics = _aggregate_metrics(closed)
    with_vol_percentiles = _with_volatility_percentiles(closed)
    price_bands = _bucket_report(closed, key_fn=_price_band_bucket, value_key="entry_price")
    return {
        "total_closed_trades": len(closed),
        "overall_win_rate": metrics["win_rate"],
        "overall_roi": metrics["roi"],
        "overall_expectancy": metrics["expectancy"],
        "edge_buckets": _bucket_report(closed, key_fn=_edge_bucket, value_key="edge"),
        "price_buckets": price_bands,
        "price_band_analysis": price_bands,
        "probability_buckets": _bucket_report(closed, key_fn=_probability_bucket, value_key="model_probability"),
        "lead_time_buckets": _bucket_report(closed, key_fn=_lead_time_bucket, value_key="seconds_to_expiry_at_entry"),
        "btc_regime_analysis": {
            "btc_return_5m": _bucket_report(closed, key_fn=lambda item: _return_regime_bucket(item.get("btc_return_5m")), value_key="btc_return_5m"),
            "btc_return_15m": _bucket_report(closed, key_fn=lambda item: _return_regime_bucket(item.get("btc_return_15m")), value_key="btc_return_15m"),
            "btc_return_1h": _bucket_report(closed, key_fn=lambda item: _return_regime_bucket(item.get("btc_return_1h")), value_key="btc_return_1h"),
            "volatility_percentile": _bucket_report(with_vol_percentiles, key_fn=lambda item: _volatility_percentile_bucket(item.get("btc_volatility_percentile")), value_key="btc_volatility_percentile"),
        },
        "hour_of_day": _bucket_report(closed, key_fn=lambda item: str(item.get("hour_of_day_utc")), value_key="hour_of_day_utc"),
        "direction": _bucket_report(closed, key_fn=lambda item: str(item.get("direction")), value_key="direction"),
        "window_size": _bucket_report(closed, key_fn=lambda item: str(item.get("window_minutes")), value_key="window_minutes"),
    }


def calibration_report(
    db_path: str = DEFAULT_DB_PATH,
    *,
    refresh_features: bool = True,
    bucket_width: float = PROBABILITY_BUCKET_WIDTH,
) -> dict[str, Any]:
    store = ShortCryptoDiagnosticsStore(db_path)
    if refresh_features:
        store.sync_features()
    features = [item for item in store.load_features() if item.get("outcome") in {"won", "lost"}]
    buckets: dict[str, dict[str, Any]] = {}
    for feature in features:
        prob = _float_or_none(feature.get("model_probability"))
        if prob is None:
            continue
        label = _probability_bucket_label(prob, bucket_width=bucket_width)
        bucket = buckets.setdefault(label, {"bucket": label, "predicted_sum": 0.0, "wins": 0, "count": 0})
        bucket["count"] += 1
        bucket["predicted_sum"] += prob
        if feature.get("outcome") == "won":
            bucket["wins"] += 1
    calibration = []
    for label in sorted(buckets, key=_bucket_sort_key):
        bucket = buckets[label]
        count = bucket["count"]
        calibration.append(
            {
                "bucket": label,
                "predicted": round(bucket["predicted_sum"] / count, 4) if count else None,
                "actual": round(bucket["wins"] / count, 4) if count else None,
                "count": count,
            }
        )
    return {"calibration": calibration}


def edge_discovery_report(
    db_path: str = DEFAULT_DB_PATH,
    *,
    refresh_features: bool = True,
    min_segment_trades: int = DEFAULT_MIN_SEGMENT_TRADES,
) -> dict[str, Any]:
    store = ShortCryptoDiagnosticsStore(db_path)
    if refresh_features:
        store.sync_features()
    features = [item for item in store.load_features() if item.get("outcome") in {"won", "lost"}]
    segments = _candidate_segments(features, min_segment_trades=min_segment_trades)
    ranked = sorted(segments, key=lambda item: (item.get("roi") if item.get("roi") is not None else -999, item.get("win_rate") or 0), reverse=True)
    best = [item for item in ranked if (item.get("roi") or 0) > 0][:5]
    worst = sorted(
        [item for item in segments if item.get("roi") is not None],
        key=lambda item: (item.get("roi") or 0, item.get("win_rate") or 0),
    )[:5]
    return {
        "min_segment_trades": min_segment_trades,
        "best_segments": best,
        "worst_segments": worst,
        "segments_evaluated": len(segments),
    }


def recommendation_report(
    db_path: str = DEFAULT_DB_PATH,
    *,
    refresh_features: bool = True,
    min_segment_trades: int = DEFAULT_MIN_SEGMENT_TRADES,
) -> dict[str, Any]:
    calibration = calibration_report(db_path, refresh_features=refresh_features)
    discovery = edge_discovery_report(db_path, refresh_features=refresh_features, min_segment_trades=min_segment_trades)
    recommendations = []
    for item in calibration["calibration"]:
        if item["count"] < min_segment_trades:
            continue
        predicted = item.get("predicted")
        actual = item.get("actual")
        if predicted is None or actual is None:
            continue
        if actual + 0.05 < predicted:
            recommendations.append(
                {
                    "trade_bucket": item["bucket"],
                    "status": "disable",
                    "reason": f"observed {round(actual * 100, 1)}% win rate vs predicted {round(predicted * 100, 1)}%",
                }
            )
        elif actual >= predicted and actual >= 0.55:
            recommendations.append(
                {
                    "trade_bucket": item["bucket"],
                    "status": "enable",
                    "reason": f"observed {round(actual * 100, 1)}% win rate meets or beats model",
                }
            )
    if discovery["best_segments"]:
        top = discovery["best_segments"][0]
        recommendations.insert(
            0,
            {
                "trade_bucket": top["segment"],
                "status": "prefer",
                "reason": f"segment roi {round((top.get('roi') or 0) * 100, 1)}% over {top['trades']} trades",
            },
        )
    if discovery["worst_segments"]:
        bottom = discovery["worst_segments"][0]
        recommendations.append(
            {
                "trade_bucket": bottom["segment"],
                "status": "disable",
                "reason": f"segment roi {round((bottom.get('roi') or 0) * 100, 1)}% over {bottom['trades']} trades",
            }
        )
    return {"recommendations": recommendations}


def verbose_report_extensions(
    db_path: str = DEFAULT_DB_PATH,
    *,
    refresh_features: bool = True,
    min_segment_trades: int = DEFAULT_MIN_SEGMENT_TRADES,
) -> dict[str, Any]:
    diagnostics = diagnostics_report(db_path, refresh_features=refresh_features)
    calibration = calibration_report(db_path, refresh_features=False)
    discovery = edge_discovery_report(db_path, refresh_features=False, min_segment_trades=min_segment_trades)
    recommendations = recommendation_report(db_path, refresh_features=False, min_segment_trades=min_segment_trades)
    return {
        "diagnostics_summary": {
            "total_closed_trades": diagnostics["total_closed_trades"],
            "overall_win_rate": diagnostics["overall_win_rate"],
            "overall_roi": diagnostics["overall_roi"],
            "overall_expectancy": diagnostics.get("overall_expectancy"),
            "probability_buckets": diagnostics["probability_buckets"],
            "lead_time_buckets": diagnostics.get("lead_time_buckets", {}),
            "price_band_analysis": diagnostics.get("price_band_analysis", {}),
            "btc_regime_analysis": diagnostics.get("btc_regime_analysis", {}),
            "direction": diagnostics["direction"],
        },
        "calibration_summary": calibration["calibration"],
        "best_segment_summary": discovery["best_segments"],
        "worst_segment_summary": discovery["worst_segments"],
        "recommendation": recommendations["recommendations"][0] if recommendations["recommendations"] else None,
        "recommendations": recommendations["recommendations"],
    }


def _closed_trades_with_settlements(store: ShortCryptoPaperStore) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    with store.connect() as conn:
        trades = [dict(row) for row in conn.execute("SELECT * FROM paper_trades WHERE status IN ('won', 'lost')").fetchall()]
        settlements = [dict(row) for row in conn.execute("SELECT * FROM paper_settlements").fetchall()]
    settlement_by_trade = {int(item["trade_id"]): item for item in settlements}
    return [(trade, settlement_by_trade[int(trade["id"])]) for trade in trades if int(trade["id"]) in settlement_by_trade]


def _extract_bid_ask(raw: dict[str, Any], trade: dict[str, Any]) -> tuple[float | None, float | None]:
    book = raw.get("book") if isinstance(raw.get("book"), dict) else {}
    side = "yes" if str(trade.get("side", "YES")).upper() == "YES" else "no"
    container = book.get(side) or {}
    bid = _best_price(container.get("bids") or [], reverse=True)
    ask = _best_price(container.get("asks") or [], reverse=False)
    return bid, ask


def _best_price(levels: list[Any], *, reverse: bool) -> float | None:
    parsed: list[float] = []
    for level in levels:
        price = None
        if isinstance(level, dict):
            price = level.get("price")
        elif isinstance(level, (list, tuple)) and level:
            price = level[0]
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if value > 1.0:
            value /= 100.0
        if 0.0 < value < 1.0:
            parsed.append(value)
    if not parsed:
        return None
    return sorted(parsed, reverse=reverse)[0]


def _btc_context_at_entry(entry_ts: float, spot_price: Any) -> dict[str, Any]:
    context = {
        "btc_price_at_entry": _float_or_none(spot_price),
        "btc_price_1m_before": None,
        "btc_price_5m_before": None,
        "btc_price_15m_before": None,
        "btc_price_1h_before": None,
        "btc_return_1m": None,
        "btc_return_5m": None,
        "btc_return_15m": None,
        "btc_return_1h": None,
        "btc_volatility_5m": None,
    }
    candles = _fetch_btc_candles_around(entry_ts)
    if not candles:
        return context
    prices = [float(item[4]) for item in candles if len(item) >= 5]
    if not prices:
        return context
    context["btc_price_at_entry"] = context["btc_price_at_entry"] or prices[-1]
    if len(prices) >= 2:
        context["btc_price_1m_before"] = prices[-2]
        if context["btc_price_at_entry"]:
            context["btc_return_1m"] = (context["btc_price_at_entry"] - prices[-2]) / prices[-2]
    if len(prices) >= 6:
        context["btc_price_5m_before"] = prices[-6]
        if context["btc_price_at_entry"]:
            context["btc_return_5m"] = (context["btc_price_at_entry"] - prices[-6]) / prices[-6]
    if len(prices) >= 16:
        context["btc_price_15m_before"] = prices[-16]
        if context["btc_price_at_entry"]:
            context["btc_return_15m"] = (context["btc_price_at_entry"] - prices[-16]) / prices[-16]
    if len(prices) >= 61:
        context["btc_price_1h_before"] = prices[-61]
        if context["btc_price_at_entry"]:
            context["btc_return_1h"] = (context["btc_price_at_entry"] - prices[-61]) / prices[-61]
    if len(prices) >= 6:
        returns = [(prices[idx] - prices[idx - 1]) / prices[idx - 1] for idx in range(len(prices) - 5, len(prices)) if prices[idx - 1]]
        if len(returns) >= 2:
            context["btc_volatility_5m"] = statistics.pstdev(returns)
    return context


def _fetch_btc_candles_around(entry_ts: float) -> list[list[Any]]:
    start = int(entry_ts - 61 * 60)
    end = int(entry_ts + 60)
    url = (
        "https://api.exchange.coinbase.com/products/BTC-USD/candles"
        f"?granularity=60&start={start}&end={end}"
    )
    try:
        request = Request(url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, list):
            return sorted(payload, key=lambda item: item[0])
    except Exception:
        return []
    return []


def _aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"win_rate": None, "roi": None, "expectancy": None, "count": 0, "pnl": 0.0}
    wins = sum(1 for row in rows if row.get("outcome") == "won")
    pnl = sum(float(row.get("pnl") or 0.0) for row in rows)
    cost = sum(float(row.get("entry_price") or 0.0) * 1.0 for row in rows)
    return {
        "count": len(rows),
        "win_rate": wins / len(rows),
        "roi": pnl / cost if cost else None,
        "expectancy": pnl / len(rows),
        "pnl": pnl,
    }


def _bucket_report(
    rows: list[dict[str, Any]],
    *,
    key_fn,
    value_key: str,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        grouped.setdefault(str(key), []).append(row)
    report: dict[str, Any] = {}
    for key, items in sorted(grouped.items(), key=lambda item: item[0]):
        metrics = _aggregate_metrics(items)
        bucket = {
            "trades": metrics["count"],
            "win_rate": metrics["win_rate"],
            "roi": metrics["roi"],
            "expectancy": metrics["expectancy"],
            "pnl": metrics["pnl"],
        }
        avg_value = _avg_numeric([item.get(value_key) for item in items])
        if avg_value is not None:
            bucket["avg_" + value_key] = avg_value
        report[key] = bucket
    return report


def _candidate_segments(features: list[dict[str, Any]], *, min_segment_trades: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def add_segment(name: str, rows: list[dict[str, Any]]) -> None:
        if len(rows) < min_segment_trades:
            return
        metrics = _aggregate_metrics(rows)
        candidates.append(
            {
                "segment": name,
                "trades": metrics["count"],
                "win_rate": metrics["win_rate"],
                "roi": metrics["roi"],
                "expectancy": metrics["expectancy"],
                "pnl": metrics["pnl"],
            }
        )

    for threshold in (0.55, 0.60, 0.65):
        rows = [item for item in features if (item.get("model_probability") or 0) > threshold]
        add_segment(f"model_probability>{threshold:.2f}", rows)
    for threshold in (0.48, 0.50, 0.52):
        rows = [item for item in features if item.get("ask") is not None and item["ask"] <= threshold]
        add_segment(f"ask<={threshold:.2f}", rows)
    for hour in sorted({item.get("hour_of_day_utc") for item in features if item.get("hour_of_day_utc") is not None}):
        rows = [item for item in features if item.get("hour_of_day_utc") == hour]
        add_segment(f"hour_utc={hour}", rows)
    for direction in ("up", "down"):
        rows = [item for item in features if str(item.get("direction")).lower() == direction]
        add_segment(f"direction={direction}", rows)
    vol_rows = [item for item in features if item.get("btc_volatility_5m") is not None]
    if vol_rows:
        vols = sorted(item["btc_volatility_5m"] for item in vol_rows)
        median = vols[len(vols) // 2]
        add_segment("btc_volatility_5m<=median", [item for item in vol_rows if item["btc_volatility_5m"] <= median])
        add_segment("btc_volatility_5m>median", [item for item in vol_rows if item["btc_volatility_5m"] > median])
    for label, low, high in (("edge_0_2pct", 0.0, 0.02), ("edge_2_5pct", 0.02, 0.05)):
        rows = [item for item in features if item.get("edge") is not None and low <= item["edge"] < high]
        add_segment(label, rows)
    rows = [item for item in features if item.get("edge") is not None and item["edge"] >= 0.05]
    add_segment("edge_gt_5pct", rows)
    return candidates


def _lead_time_bucket(item: dict[str, Any]) -> str | None:
    seconds = _float_or_none(item.get("seconds_to_expiry_at_entry"))
    if seconds is None:
        return None
    minutes = seconds / 60.0
    if minutes < 0:
        return None
    if minutes <= 30:
        return "0-30"
    if minutes <= 60:
        return "30-60"
    if minutes <= 120:
        return "60-120"
    if minutes <= 240:
        return "120-240"
    return ">240"


def _price_band_bucket(item: dict[str, Any]) -> str | None:
    price = _float_or_none(item.get("entry_price"))
    if price is None:
        return None
    if price <= 0.45:
        return "<=0.45"
    if price <= 0.50:
        return "0.45-0.50"
    if price <= 0.55:
        return "0.50-0.55"
    if price <= 0.60:
        return "0.55-0.60"
    return ">0.60"


def _return_regime_bucket(value: Any) -> str | None:
    ret = _float_or_none(value)
    if ret is None:
        return None
    if ret <= -0.001:
        return "negative"
    if ret >= 0.001:
        return "positive"
    return "flat"


def _with_volatility_percentiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vol_rows = [(idx, _float_or_none(row.get("btc_volatility_5m"))) for idx, row in enumerate(rows)]
    vol_rows = [(idx, value) for idx, value in vol_rows if value is not None]
    if not vol_rows:
        return []
    ranked = sorted(vol_rows, key=lambda item: item[1])
    denom = max(1, len(ranked) - 1)
    percentiles = {idx: rank / denom for rank, (idx, _value) in enumerate(ranked)}
    enriched = []
    for idx, row in enumerate(rows):
        if idx not in percentiles:
            continue
        item = dict(row)
        item["btc_volatility_percentile"] = percentiles[idx]
        enriched.append(item)
    return enriched


def _volatility_percentile_bucket(value: Any) -> str | None:
    percentile = _float_or_none(value)
    if percentile is None:
        return None
    if percentile <= 0.25:
        return "0-25"
    if percentile <= 0.50:
        return "25-50"
    if percentile <= 0.75:
        return "50-75"
    return "75-100"


def _edge_bucket(item: dict[str, Any]) -> str | None:
    edge = _float_or_none(item.get("edge"))
    if edge is None:
        return None
    if edge < 0:
        return "<0"
    lower = math.floor(edge / EDGE_BUCKET_WIDTH) * EDGE_BUCKET_WIDTH
    upper = lower + EDGE_BUCKET_WIDTH
    return f"{lower:.2f}-{upper:.2f}"


def _price_bucket(item: dict[str, Any]) -> str | None:
    return _price_band_bucket(item)


def _probability_bucket(item: dict[str, Any]) -> str | None:
    prob = _float_or_none(item.get("model_probability"))
    if prob is None:
        return None
    return _probability_bucket_label(prob)


def _probability_bucket_label(prob: float, *, bucket_width: float = PROBABILITY_BUCKET_WIDTH) -> str:
    lower_pct = int(math.floor(prob / bucket_width) * bucket_width * 100)
    upper_pct = lower_pct + int(bucket_width * 100)
    return f"{lower_pct}-{upper_pct}%"


def _bucket_sort_key(label: str) -> int:
    try:
        return int(label.split("-")[0].replace("%", ""))
    except ValueError:
        return 0


def _avg(values: list[Any]) -> float | None:
    return _avg_numeric(values)


def _avg_numeric(values: list[Any]) -> float | None:
    nums: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            nums.append(float(value))
        except (TypeError, ValueError):
            continue
    return sum(nums) / len(nums) if nums else None


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
