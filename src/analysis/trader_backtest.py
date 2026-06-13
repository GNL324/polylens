from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from src.analysis.wallet_activity import WalletActivityEvent, WalletActivityExport, export_wallet_activity


@dataclass
class TraderBacktestTrade:
    wallet: str
    market_id: str
    market_title: str
    asset: str
    side: str
    entry_timestamp: float
    exit_timestamp: float | None
    entry_price: float
    exit_price: float | None
    shares: float
    notional: float
    pnl: float | None
    roi: float | None
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraderBacktestReport:
    wallet: str
    trade_count: int
    closed_trades: int
    win_rate: float
    total_pnl: float
    realized_pnl: float
    roi: float
    avg_trade_return: float
    max_drawdown: float
    largest_win: float
    largest_loss: float
    avg_hold_minutes: float
    open_trades: int
    unresolved_count: int
    open_notional: float
    merge_count: int
    redeem_count: int
    by_asset: dict[str, dict[str, Any]]
    by_side: dict[str, dict[str, Any]]
    by_duration: dict[str, dict[str, Any]]
    by_strategy_profile: dict[str, dict[str, Any]]
    trades: list[TraderBacktestTrade] = field(default_factory=list)

    def to_dict(self, include_trades: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        if not include_trades:
            payload.pop("trades", None)
        return payload


@dataclass
class _Lot:
    wallet: str
    market_id: str
    market_title: str
    asset: str
    side: str
    timestamp: float
    price: float
    remaining_shares: float
    remaining_cost: float
    duration: str


def build_trader_backtest_report(
    wallet: str,
    limit: int | None = None,
    source: Any | None = None,
) -> TraderBacktestReport:
    export = export_wallet_activity(wallet=wallet, limit=limit, source=source, store=False)
    return backtest_wallet_activity(export)


def backtest_wallet_activity(export: WalletActivityExport | dict[str, Any] | list[Any]) -> TraderBacktestReport:
    wallet, events = _coerce_events(export)
    lots: dict[tuple[str, str], list[_Lot]] = defaultdict(list)
    trades: list[TraderBacktestTrade] = []
    merge_count = 0
    redeem_count = 0

    for event in sorted(events, key=lambda item: (_timestamp(item), item.tx_hash, item.market_id, item.side, item.action)):
        market_id = _market_key(event)
        side = event.side or "unknown"
        key = (market_id, side)
        if event.action == "buy":
            shares = max(0.0, float(event.shares or 0.0))
            if shares <= 0:
                continue
            cost = _event_notional(event, default=shares * max(0.0, float(event.price or 0.0)))
            lots[key].append(
                _Lot(
                    wallet=event.wallet or wallet,
                    market_id=market_id,
                    market_title=event.market_title or "unknown",
                    asset=(event.asset or "OTHER").upper(),
                    side=side,
                    timestamp=_timestamp(event),
                    price=_unit_price(cost, shares, fallback=event.price),
                    remaining_shares=shares,
                    remaining_cost=cost,
                    duration=_duration_bucket(event),
                )
            )
        elif event.action in {"sell", "redeem"}:
            if event.action == "redeem":
                redeem_count += 1
            shares = max(0.0, float(event.shares or 0.0))
            lot_matches = lots.get(key, [])
            if event.action == "redeem" and side in {"", "unknown"}:
                market_lots_by_side = {
                    lot_side: [lot for lot in lot_list if lot.remaining_shares > 1e-9]
                    for (lot_market, lot_side), lot_list in lots.items()
                    if lot_market == market_id
                }
                open_sides = [lot_side for lot_side, side_lots in market_lots_by_side.items() if side_lots]
                lot_matches = market_lots_by_side[open_sides[0]] if len(open_sides) == 1 else []
            if shares <= 0:
                shares = sum(lot.remaining_shares for lot in lot_matches)
            proceeds = _exit_proceeds(event, shares)
            if proceeds is None or shares <= 0:
                continue
            trades.extend(_close_lots(lot_matches, event, shares, proceeds, "redeemed" if event.action == "redeem" else "sold"))
        elif event.action == "merge":
            merge_count += 1

    for lot_list in lots.values():
        for lot in lot_list:
            if lot.remaining_shares <= 1e-9:
                continue
            trades.append(
                TraderBacktestTrade(
                    wallet=lot.wallet,
                    market_id=lot.market_id,
                    market_title=lot.market_title,
                    asset=lot.asset,
                    side=lot.side,
                    entry_timestamp=lot.timestamp,
                    exit_timestamp=None,
                    entry_price=round(lot.price, 6),
                    exit_price=None,
                    shares=round(lot.remaining_shares, 6),
                    notional=round(lot.remaining_cost, 6),
                    pnl=None,
                    roi=None,
                    outcome="open",
                )
            )

    closed = [trade for trade in trades if trade.pnl is not None]
    open_trades = [trade for trade in trades if trade.pnl is None]
    realized_cost = sum(trade.notional for trade in closed)
    realized_pnl = round(sum(float(trade.pnl or 0.0) for trade in closed), 6)
    win_rate = _ratio(sum(1 for trade in closed if (trade.pnl or 0.0) > 0), len(closed))
    returns = [float(trade.roi or 0.0) for trade in closed]
    holds = [
        max(0.0, float(trade.exit_timestamp or 0.0) - float(trade.entry_timestamp or 0.0)) / 60.0
        for trade in closed
        if trade.exit_timestamp is not None
    ]
    return TraderBacktestReport(
        wallet=wallet,
        trade_count=len(trades),
        closed_trades=len(closed),
        win_rate=round(win_rate, 6),
        total_pnl=realized_pnl,
        realized_pnl=realized_pnl,
        roi=round(_ratio(realized_pnl, realized_cost), 6),
        avg_trade_return=round(sum(returns) / len(returns), 6) if returns else 0.0,
        max_drawdown=round(_max_drawdown(closed), 6),
        largest_win=round(max([0.0, *[float(trade.pnl or 0.0) for trade in closed]]), 6),
        largest_loss=round(min([0.0, *[float(trade.pnl or 0.0) for trade in closed]]), 6),
        avg_hold_minutes=round(sum(holds) / len(holds), 6) if holds else 0.0,
        open_trades=len(open_trades),
        unresolved_count=len(open_trades),
        open_notional=round(sum(trade.notional for trade in open_trades), 6),
        merge_count=merge_count,
        redeem_count=redeem_count,
        by_asset=_segment(trades, lambda trade: trade.asset or "OTHER"),
        by_side=_segment(trades, lambda trade: trade.side or "unknown"),
        by_duration=_segment(trades, lambda trade: _trade_duration_bucket(trade)),
        by_strategy_profile=_segment(trades, _strategy_profile),
        trades=trades,
    )


def _close_lots(lots: list[_Lot], event: WalletActivityEvent, shares: float, proceeds: float, outcome: str) -> list[TraderBacktestTrade]:
    closed: list[TraderBacktestTrade] = []
    remaining = shares
    proceeds_per_share = proceeds / shares if shares > 0 else 0.0
    for lot in lots:
        if remaining <= 1e-9:
            break
        if lot.remaining_shares <= 1e-9:
            continue
        closed_shares = min(lot.remaining_shares, remaining)
        cost_ratio = closed_shares / lot.remaining_shares if lot.remaining_shares else 0.0
        cost = lot.remaining_cost * cost_ratio
        realized = closed_shares * proceeds_per_share
        pnl = realized - cost
        closed.append(
            TraderBacktestTrade(
                wallet=lot.wallet,
                market_id=lot.market_id,
                market_title=lot.market_title,
                asset=lot.asset,
                side=lot.side,
                entry_timestamp=lot.timestamp,
                exit_timestamp=_timestamp(event),
                entry_price=round(lot.price, 6),
                exit_price=round(proceeds_per_share, 6),
                shares=round(closed_shares, 6),
                notional=round(cost, 6),
                pnl=round(pnl, 6),
                roi=round(_ratio(pnl, cost), 6),
                outcome=outcome,
            )
        )
        lot.remaining_shares -= closed_shares
        lot.remaining_cost -= cost
        remaining -= closed_shares
    return closed


def _coerce_events(export: WalletActivityExport | dict[str, Any] | list[Any]) -> tuple[str, list[WalletActivityEvent]]:
    if isinstance(export, WalletActivityExport):
        return export.wallet, list(export.events)
    if isinstance(export, dict):
        wallet = str(export.get("wallet") or "")
        rows = export.get("events") or []
    else:
        wallet = ""
        rows = export
    events: list[WalletActivityEvent] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, WalletActivityEvent):
            events.append(row)
        elif isinstance(row, dict):
            events.append(_event_from_dict(row, wallet))
    if not wallet and events:
        wallet = events[0].wallet
    return wallet.lower(), events


def _event_from_dict(row: dict[str, Any], default_wallet: str) -> WalletActivityEvent:
    return WalletActivityEvent(
        wallet=str(row.get("wallet") or default_wallet).lower(),
        timestamp=_safe_float(row.get("timestamp")),
        event_type=str(row.get("event_type") or row.get("action") or ""),
        market_id=str(row.get("market_id") or ""),
        condition_id=str(row.get("condition_id") or ""),
        market_slug=str(row.get("market_slug") or ""),
        market_title=str(row.get("market_title") or "unknown"),
        asset=str(row.get("asset") or "OTHER").upper(),
        side=str(row.get("side") or "").lower(),
        action=str(row.get("action") or "").lower(),
        shares=_safe_float(row.get("shares")),
        price=_safe_float(row.get("price")),
        amount=_safe_float(row.get("amount")),
        tx_hash=str(row.get("tx_hash") or ""),
        raw=dict(row.get("raw") or {}),
    )


def _market_key(event: WalletActivityEvent) -> str:
    return event.market_id or event.condition_id or event.market_slug or event.market_title or "unknown"


def _timestamp(event: WalletActivityEvent) -> float:
    value = _safe_float(event.timestamp)
    return value / 1000.0 if value > 10_000_000_000 else value


def _event_notional(event: WalletActivityEvent, default: float = 0.0) -> float:
    amount = _safe_float(event.amount)
    if amount > 0:
        return amount
    shares = _safe_float(event.shares)
    price = _safe_float(event.price)
    return default if shares <= 0 or price <= 0 else shares * price


def _exit_proceeds(event: WalletActivityEvent, shares: float) -> float | None:
    amount = _safe_float(event.amount)
    if amount > 0:
        return amount
    price = _safe_float(event.price)
    if price > 0 and shares > 0:
        return shares * price
    return None


def _unit_price(amount: float, shares: float, fallback: float = 0.0) -> float:
    if shares > 0 and amount > 0:
        return amount / shares
    return _safe_float(fallback)


def _duration_bucket(event: WalletActivityEvent) -> str:
    text = f"{event.market_title} {event.market_slug}".lower()
    if "5m" in text or "5-min" in text or "5 min" in text or "5 minute" in text:
        return "5m"
    if "15m" in text or "15-min" in text or "15 min" in text or "15 minute" in text:
        return "15m"
    if "hour" in text or "60m" in text or "1h" in text:
        return "hourly"
    return "unknown"


def _trade_duration_bucket(trade: TraderBacktestTrade) -> str:
    text = trade.market_title.lower()
    if "5m" in text or "5-min" in text or "5 min" in text or "5 minute" in text:
        return "5m"
    if "15m" in text or "15-min" in text or "15 min" in text or "15 minute" in text:
        return "15m"
    if "hour" in text or "60m" in text or "1h" in text:
        return "hourly"
    return "unknown"


def _strategy_profile(trade: TraderBacktestTrade) -> str:
    if trade.outcome == "open":
        return "unknown"
    title = trade.market_title.lower()
    if trade.side in {"up", "down", "yes", "no"} and ("up or down" in title or "above" in title or "below" in title):
        return "directional"
    if trade.outcome == "redeemed":
        return "inventory"
    return "unknown"


def _segment(trades: list[TraderBacktestTrade], key_fn: Any) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for trade in trades:
        key = str(key_fn(trade) or "unknown")
        bucket = buckets.setdefault(
            key,
            {"trades": 0, "closed_trades": 0, "open_trades": 0, "pnl": 0.0, "notional": 0.0, "roi": 0.0, "win_rate": 0.0},
        )
        bucket["trades"] += 1
        bucket["notional"] += trade.notional
        if trade.pnl is None:
            bucket["open_trades"] += 1
        else:
            bucket["closed_trades"] += 1
            bucket["pnl"] += trade.pnl
    for key, bucket in buckets.items():
        bucket["notional"] = round(bucket["notional"], 6)
        bucket["pnl"] = round(bucket["pnl"], 6)
        bucket["roi"] = round(_ratio(bucket["pnl"], bucket["notional"]), 6)
        wins = sum(1 for trade in trades if str(key_fn(trade) or "unknown") == key and trade.pnl is not None and trade.pnl > 0)
        bucket["win_rate"] = round(_ratio(wins, bucket["closed_trades"]), 6)
    return dict(sorted(buckets.items()))


def _max_drawdown(closed_trades: list[TraderBacktestTrade]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(closed_trades, key=lambda item: float(item.exit_timestamp or 0.0)):
        equity += float(trade.pnl or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _safe_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
