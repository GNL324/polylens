from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CLASSIFICATIONS = (
    "market_maker",
    "arbitrage_trader",
    "quantitative_directional",
    "mixed",
    "unknown",
)

OPPOSING_OUTCOMES: tuple[frozenset[str], ...] = (
    frozenset({"up", "down"}),
    frozenset({"yes", "no"}),
)

SCHEMA_VERSION = 1

INVALID_OUTCOME_INDEX = {999}


@dataclass
class WalletActivity:
    wallet_address: str
    market_title: str
    asset: str
    side: str
    action: str
    price_cents: float
    shares: float
    amount_usd: float
    timestamp: float
    market_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalletForensicsReport:
    wallet: str
    classification: str
    confidence: float
    signals: list[dict[str, Any]]
    metrics: dict[str, Any]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "wallet": self.wallet,
            "classification": self.classification,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "signals": self.signals,
            "metrics": dict(sorted(self.metrics.items())),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def summary_text(self) -> str:
        lines = [
            "Wallet Forensics Report",
            "=" * 25,
            f"Wallet: {self.wallet}",
            f"Classification: {self.classification}",
            f"Confidence: {self.confidence:.2f}",
            "",
            "Reasons:",
        ]
        lines.extend(f"  - {reason}" for reason in self.reasons)
        lines.extend(["", "Signals:"])
        for signal in self.signals:
            lines.append(f"  - {signal['name']}: {signal['explanation']}")
        lines.extend(["", "Key Metrics:"])
        for key in (
            "trade_count",
            "merge_count",
            "redeem_count",
            "overlap_count",
            "overlap_ratio",
            "arbitrage_opportunities",
            "average_sum_price",
            "btc_volume",
            "eth_volume",
            "sol_volume",
        ):
            if key in self.metrics:
                lines.append(f"  {key}: {self.metrics[key]}")
        return "\n".join(lines)


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_outcome(value: Any, record: dict[str, Any] | None = None) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if text in {"up", "down", "yes", "no"}:
        return text
    if record is not None:
        return _outcome_from_index(record)
    return ""


def _outcome_from_index(record: dict[str, Any]) -> str:
    raw_index = record.get("outcomeIndex") if record.get("outcomeIndex") is not None else record.get("outcome_index")
    if raw_index in (None, ""):
        return ""
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return ""
    if index in INVALID_OUTCOME_INDEX or index not in (0, 1):
        return ""
    title = str(record.get("title") or record.get("market_title") or record.get("slug") or record.get("market_slug") or "").lower()
    if "up or down" in title or "up/down" in title or "updown" in title:
        return "up" if index == 0 else "down"
    return "yes" if index == 0 else "no"


def _normalize_action(record: dict[str, Any]) -> str | None:
    explicit_action = str(record.get("action") or "").strip().lower()
    if explicit_action in {"buy", "sell", "redeem", "merge"}:
        return explicit_action
    activity_type = str(record.get("type") or record.get("event_type") or "").upper()
    if activity_type == "TRADE":
        side = str(record.get("side") or "").strip().lower()
        if side in {"buy", "sell"}:
            return side
        return None
    if activity_type == "REDEEM":
        return "redeem"
    if activity_type == "MERGE":
        return "merge"
    return None


def _market_key(record: dict[str, Any]) -> str:
    for key in ("conditionId", "condition_id", "market_id", "slug", "market_slug", "title", "market_title"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def infer_asset(title: str, slug: str = "", explicit_asset: str = "") -> str:
    explicit = str(explicit_asset or "").strip().upper()
    if explicit in {"BTC", "ETH", "SOL"}:
        return explicit
    text = f"{title} {slug}".upper()
    if re_search_asset(text, ("BTC", "BITCOIN")):
        return "BTC"
    if re_search_asset(text, ("ETH", "ETHEREUM")):
        return "ETH"
    if re_search_asset(text, ("SOL", "SOLANA")):
        return "SOL"
    return "OTHER"


def re_search_asset(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _price_cents(record: dict[str, Any]) -> float:
    return round(_safe_float(record.get("price")) * 100, 4)


def _amount_usd(record: dict[str, Any]) -> float:
    if record.get("amount") is not None and record.get("amount") != "":
        return round(_safe_float(record.get("amount")), 6)
    if record.get("usdcSize") is not None and record.get("usdcSize") != "":
        return round(_safe_float(record.get("usdcSize")), 6)
    size = _safe_float(record.get("size"))
    price = _safe_float(record.get("price"))
    return round(size * price, 6)


def parse_activity_record(record: dict[str, Any], wallet_address: str) -> WalletActivity | None:
    action = _normalize_action(record)
    if action is None:
        return None
    market_id = _market_key(record)
    title = str(record.get("title") or record.get("market_title") or record.get("slug") or record.get("market_slug") or market_id or "unknown")
    slug = str(record.get("slug") or record.get("market_slug") or "")
    outcome = _normalize_outcome(record.get("outcome") or record.get("side"), record)
    shares = _safe_float(record.get("size") if record.get("size") is not None else record.get("shares"))
    amount_usd = _amount_usd(record)
    timestamp = _safe_float(record.get("timestamp"))
    return WalletActivity(
        wallet_address=wallet_address,
        market_title=title,
        asset=infer_asset(title, slug, explicit_asset=str(record.get("asset") or "")),
        side=outcome,
        action=action,
        price_cents=_price_cents(record),
        shares=shares,
        amount_usd=amount_usd,
        timestamp=timestamp,
        market_id=market_id,
    )


def parse_activity_payload(payload: Any, wallet_address: str) -> list[WalletActivity]:
    rows = _extract_activity_rows(payload)
    activities: list[WalletActivity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_activity_record(row, wallet_address)
        if parsed is not None:
            activities.append(parsed)
    activities.sort(key=lambda item: (item.timestamp, item.market_id, item.market_title, item.action, item.side))
    return activities


def _extract_activity_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("events", "activities", "activity", "payload"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("payload"), list):
                return value["payload"]
    raise ValueError("input JSON must be a list of activity records or an object with an activities list")


def load_wallet_activities(input_json: str | Path, wallet: str | None = None) -> tuple[list[WalletActivity], str]:
    path = Path(input_json)
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    wallet_address = wallet or ""
    if isinstance(payload, dict):
        wallet_address = str(payload.get("wallet") or payload.get("wallet_address") or wallet_address).strip()
    if not wallet_address:
        raise ValueError("wallet address is required (--wallet or wallet field in input JSON)")
    activities = parse_activity_payload(payload, wallet_address)
    return activities, wallet_address


def _outcomes_opposed(left: str, right: str) -> bool:
    if not left or not right or left == right:
        return False
    pair = {left, right}
    return any(pair.issubset(group) for group in OPPOSING_OUTCOMES)


def _is_overlap_market(outcomes: set[str]) -> bool:
    normalized = {item for item in outcomes if item}
    if len(normalized) < 2:
        return False
    for group in OPPOSING_OUTCOMES:
        if group.issubset(normalized):
            return True
    return False


def _activity_market_key(activity: WalletActivity) -> str:
    return activity.market_id or activity.market_title


def compute_basic_metrics(activities: list[WalletActivity]) -> dict[str, Any]:
    trade_actions = {"buy", "sell"}
    counts = {"buy": 0, "sell": 0, "redeem": 0, "merge": 0}
    volumes = {"buy": 0.0, "sell": 0.0, "redeem": 0.0, "merge": 0.0}
    asset_volumes = {"BTC": 0.0, "ETH": 0.0, "SOL": 0.0, "OTHER": 0.0}
    market_outcomes: dict[str, set[str]] = defaultdict(set)
    market_volume: dict[str, float] = defaultdict(float)

    market_titles: dict[str, str] = {}

    for activity in activities:
        if activity.action in counts:
            counts[activity.action] += 1
            volumes[activity.action] += activity.amount_usd
        asset_volumes[activity.asset] = round(asset_volumes.get(activity.asset, 0.0) + activity.amount_usd, 6)
        market_key = _activity_market_key(activity)
        market_titles.setdefault(market_key, activity.market_title)
        if activity.side:
            market_outcomes[market_key].add(activity.side)
        market_volume[market_key] += activity.amount_usd

    markets_traded = len(market_titles)
    overlap_market_ids = [market for market, outcomes in market_outcomes.items() if _is_overlap_market(outcomes)]
    overlap_markets = sorted(market_titles[market] for market in overlap_market_ids)
    overlap_count = len(overlap_market_ids)
    overlap_ratio = round(overlap_count / markets_traded, 4) if markets_traded else 0.0
    overlap_volume = round(sum(market_volume[market] for market in overlap_market_ids), 2)

    trade_count = sum(counts[action] for action in trade_actions)

    return {
        "trade_count": trade_count,
        "buy_count": counts["buy"],
        "sell_count": counts["sell"],
        "redeem_count": counts["redeem"],
        "merge_count": counts["merge"],
        "buy_volume": round(volumes["buy"], 2),
        "sell_volume": round(volumes["sell"], 2),
        "redeem_volume": round(volumes["redeem"], 2),
        "merge_volume": round(volumes["merge"], 2),
        "btc_volume": round(asset_volumes["BTC"], 2),
        "eth_volume": round(asset_volumes["ETH"], 2),
        "sol_volume": round(asset_volumes["SOL"], 2),
        "other_volume": round(asset_volumes["OTHER"], 2),
        "markets_traded": markets_traded,
        "overlap_markets": sorted(overlap_markets),
        "overlap_count": overlap_count,
        "overlap_ratio": overlap_ratio,
        "overlap_volume": overlap_volume,
    }


def compute_arbitrage_metrics(activities: list[WalletActivity]) -> dict[str, Any]:
    buy_prices: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    buy_volumes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for activity in activities:
        if activity.action != "buy" or not activity.side:
            continue
        if activity.price_cents <= 0:
            continue
        market_key = _activity_market_key(activity)
        buy_prices[market_key][activity.side].append(activity.price_cents)
        buy_volumes[market_key][activity.side] += activity.amount_usd

    sum_prices: list[float] = []
    arbitrage_opportunities = 0
    neutral_sum_count = 0
    overpay_count = 0
    arbitrage_volume = 0.0

    for market_key, sides in buy_prices.items():
        opposing_pairs: list[tuple[str, str]] = []
        side_names = set(sides)
        for group in OPPOSING_OUTCOMES:
            present = [side for side in group if side in side_names]
            if len(present) == 2:
                opposing_pairs.append((present[0], present[1]))
        if not opposing_pairs:
            continue
        for left, right in opposing_pairs:
            left_prices = sides[left]
            right_prices = sides[right]
            if not left_prices or not right_prices:
                continue
            left_avg = sum(left_prices) / len(left_prices)
            right_avg = sum(right_prices) / len(right_prices)
            sum_price = round(left_avg + right_avg, 4)
            sum_prices.append(sum_price)
            paired_volume = round(buy_volumes[market_key][left] + buy_volumes[market_key][right], 2)
            if sum_price < 100:
                arbitrage_opportunities += 1
                arbitrage_volume = round(arbitrage_volume + paired_volume, 2)
            elif abs(sum_price - 100) <= 0.01:
                neutral_sum_count += 1
            else:
                overpay_count += 1

    return {
        "arbitrage_opportunities": arbitrage_opportunities,
        "arbitrage_volume": arbitrage_volume,
        "neutral_sum_count": neutral_sum_count,
        "overpay_count": overpay_count,
        "average_sum_price": round(sum(sum_prices) / len(sum_prices), 4) if sum_prices else None,
        "minimum_sum_price": round(min(sum_prices), 4) if sum_prices else None,
        "maximum_sum_price": round(max(sum_prices), 4) if sum_prices else None,
    }


def generate_signals(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    trade_count = max(int(metrics.get("trade_count") or 0), 1)
    total_events = trade_count + int(metrics.get("redeem_count") or 0) + int(metrics.get("merge_count") or 0)
    signals: list[dict[str, Any]] = []

    merge_ratio = metrics.get("merge_count", 0) / total_events
    if metrics.get("merge_count", 0) >= 3 or merge_ratio >= 0.15:
        signals.append(
            {
                "name": "high_merge_frequency",
                "explanation": (
                    f"wallet performed {metrics.get('merge_count', 0)} merge actions "
                    f"({merge_ratio:.0%} of tracked events), suggesting inventory consolidation"
                ),
            }
        )

    if metrics.get("overlap_ratio", 0) >= 0.25 or metrics.get("overlap_count", 0) >= 2:
        signals.append(
            {
                "name": "high_overlap_frequency",
                "explanation": (
                    f"wallet held both sides in {metrics.get('overlap_count', 0)} of "
                    f"{metrics.get('markets_traded', 0)} markets "
                    f"(overlap ratio {metrics.get('overlap_ratio', 0):.2f})"
                ),
            }
        )

    rebalance_score = (
        metrics.get("buy_count", 0) + metrics.get("sell_count", 0) + metrics.get("merge_count", 0)
    ) / total_events
    if rebalance_score >= 0.7 and metrics.get("sell_count", 0) >= 2:
        signals.append(
            {
                "name": "frequent_inventory_rebalancing",
                "explanation": (
                    f"wallet alternates buys and sells across {metrics.get('markets_traded', 0)} markets "
                    f"with {metrics.get('sell_count', 0)} sells and {metrics.get('merge_count', 0)} merges"
                ),
            }
        )

    if metrics.get("overlap_count", 0) >= 1:
        signals.append(
            {
                "name": "two_sided_market_participation",
                "explanation": (
                    "wallet repeatedly participates on both outcomes within the same contracts, "
                    "including up/down or yes/no pairs"
                ),
            }
        )

    redeem_ratio = metrics.get("redeem_count", 0) / total_events
    if metrics.get("redeem_count", 0) >= 3 or redeem_ratio >= 0.2:
        signals.append(
            {
                "name": "redeem_heavy_behavior",
                "explanation": (
                    f"wallet redeemed {metrics.get('redeem_count', 0)} times "
                    f"(${metrics.get('redeem_volume', 0):,.2f} volume), indicating frequent settlement harvesting"
                ),
            }
        )

    signals.sort(key=lambda item: item["name"])
    return signals


def classify_wallet(metrics: dict[str, Any], signals: list[dict[str, Any]]) -> tuple[str, float, list[str]]:
    trade_count = int(metrics.get("trade_count") or 0)
    if trade_count == 0 and int(metrics.get("redeem_count") or 0) == 0 and int(metrics.get("merge_count") or 0) == 0:
        return "unknown", 0.0, ["insufficient activity to classify"]

    signal_names = {signal["name"] for signal in signals}
    total_events = max(trade_count + int(metrics.get("redeem_count") or 0) + int(metrics.get("merge_count") or 0), 1)

    scores: dict[str, float] = {
        "market_maker": 0.0,
        "arbitrage_trader": 0.0,
        "quantitative_directional": 0.0,
    }
    reasons_by_label: dict[str, list[str]] = defaultdict(list)

    merge_ratio = metrics.get("merge_count", 0) / total_events
    redeem_ratio = metrics.get("redeem_count", 0) / total_events
    overlap_ratio = float(metrics.get("overlap_ratio") or 0)

    if metrics.get("merge_count", 0) >= 3 or merge_ratio >= 0.15:
        scores["market_maker"] += 0.34
        reasons_by_label["market_maker"].append("high merge volume")
    if overlap_ratio >= 0.25:
        scores["market_maker"] += 0.33
        reasons_by_label["market_maker"].append("holds both sides of contracts")
    if metrics.get("redeem_count", 0) >= 2 or redeem_ratio >= 0.15:
        scores["market_maker"] += 0.25
        reasons_by_label["market_maker"].append("frequent inventory consolidation")
    if "high_merge_frequency" in signal_names and metrics.get("merge_count", 0) >= 3:
        scores["market_maker"] += 0.05
    if "redeem_heavy_behavior" in signal_names and metrics.get("redeem_count", 0) >= 3:
        scores["market_maker"] += 0.05

    arb_pairs = (
        int(metrics.get("arbitrage_opportunities") or 0)
        + int(metrics.get("neutral_sum_count") or 0)
        + int(metrics.get("overpay_count") or 0)
    )
    if metrics.get("arbitrage_opportunities", 0) >= 2:
        scores["arbitrage_trader"] += 0.45
        reasons_by_label["arbitrage_trader"].append("repeated sub-100 sum_price purchases")
    if arb_pairs > 0 and metrics.get("arbitrage_opportunities", 0) / arb_pairs >= 0.5:
        scores["arbitrage_trader"] += 0.25
        reasons_by_label["arbitrage_trader"].append("price-sum edge dominates two-sided buys")
    if metrics.get("arbitrage_volume", 0) >= 50:
        scores["arbitrage_trader"] += 0.15
        reasons_by_label["arbitrage_trader"].append("meaningful arbitrage notional")

    if overlap_ratio < 0.15 and merge_ratio < 0.05:
        scores["quantitative_directional"] += 0.35
        reasons_by_label["quantitative_directional"].append("mostly one-sided participation")
    if metrics.get("buy_count", 0) >= metrics.get("sell_count", 0) and overlap_ratio < 0.2:
        scores["quantitative_directional"] += 0.25
        reasons_by_label["quantitative_directional"].append("low merge and overlap activity")
    if metrics.get("overlap_count", 0) == 0:
        scores["quantitative_directional"] += 0.2
        reasons_by_label["quantitative_directional"].append("no two-sided contract exposure")

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_label, top_score = ranked[0]
    second_score = ranked[1][1]

    if top_score < 0.25:
        return "unknown", round(min(top_score, 0.24), 2), ["insufficient dominant strategy signals"]

    if top_score >= 0.45 and second_score >= 0.35:
        mixed_reasons: list[str] = []
        for label, score in ranked:
            if score >= 0.35:
                mixed_reasons.extend(reasons_by_label.get(label, [])[:2])
        return "mixed", round(min(0.95, (top_score + second_score) / 2), 2), mixed_reasons[:5] or ["significant evidence across categories"]

    confidence = round(min(0.95, 0.45 + top_score * 0.55), 2)
    reasons = reasons_by_label.get(top_label, [])[:5] or [f"strongest evidence for {top_label.replace('_', ' ')}"]
    return top_label, confidence, reasons


def analyze_wallet_forensics(wallet: str, activities: list[WalletActivity]) -> WalletForensicsReport:
    basic = compute_basic_metrics(activities)
    arbitrage = compute_arbitrage_metrics(activities)
    metrics = {**basic, **arbitrage}
    signals = generate_signals(metrics)
    classification, confidence, reasons = classify_wallet(metrics, signals)
    return WalletForensicsReport(
        wallet=wallet,
        classification=classification,
        confidence=confidence,
        signals=signals,
        metrics=metrics,
        reasons=reasons,
    )


def build_wallet_forensics_report(input_json: str | Path, wallet: str | None = None) -> WalletForensicsReport:
    activities, wallet_address = load_wallet_activities(input_json, wallet=wallet)
    return analyze_wallet_forensics(wallet_address, activities)
