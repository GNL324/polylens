from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ExecutionAction = Literal["buy", "sell"]
ExecutionStatus = Literal["filled", "partial", "rejected"]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class PaperExecutionConfig:
    max_market_data_age_seconds: float = 300.0
    default_spread: float = 0.02
    default_slippage_bps: float = 10.0
    liquidity_participation_cap: float = 0.25
    allow_partial_fills: bool = True
    now: datetime | None = None

    @classmethod
    def from_env(cls) -> PaperExecutionConfig:
        return cls(
            max_market_data_age_seconds=_env_float("POLYLENS_PAPER_MAX_MARKET_DATA_AGE", 300.0),
            default_spread=_env_float("POLYLENS_PAPER_DEFAULT_SPREAD", 0.02),
            default_slippage_bps=_env_float("POLYLENS_PAPER_DEFAULT_SLIPPAGE_BPS", 10.0),
            liquidity_participation_cap=_env_float("POLYLENS_PAPER_LIQUIDITY_CAP", 0.25),
            allow_partial_fills=_env_bool("POLYLENS_PAPER_ALLOW_PARTIAL_FILLS", True),
        )

    @classmethod
    def zero_friction(cls, *, now: datetime | None = None) -> PaperExecutionConfig:
        """Deterministic fills for unit tests without market metadata."""
        return cls(
            max_market_data_age_seconds=86400.0 * 365,
            default_spread=0.0,
            default_slippage_bps=0.0,
            liquidity_participation_cap=1.0,
            allow_partial_fills=True,
            now=now,
        )


@dataclass
class MarketQuote:
    best_bid_yes: float | None = None
    best_ask_yes: float | None = None
    best_bid_no: float | None = None
    best_ask_no: float | None = None
    mid_price: float | None = None
    last_price: float | None = None
    liquidity: float | None = None
    volume: float | None = None
    market_status: str | None = None
    expiry_time: datetime | None = None
    quote_timestamp: datetime | None = None
    metadata_notes: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    status: ExecutionStatus
    fill_price: float | None
    filled_stake: float
    requested_stake: float
    shares: float
    slippage: float
    spread_paid: float
    quoted_mid: float | None
    reason: str | None = None
    metadata_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_market_quote(row: dict[str, Any], *, config: PaperExecutionConfig) -> MarketQuote:
    notes: list[str] = []
    mid = _first_float(row, "mid_price", "mid", "implied_prob", "entry_price", "price", "last_price")
    last = _first_float(row, "last_price", "last", "entry_price", "price")
    spread = _first_float(row, "spread")
    bid_yes = _first_float(row, "best_bid", "yes_bid", "bid", "best_bid_yes")
    ask_yes = _first_float(row, "best_ask", "yes_ask", "ask", "best_ask_yes")
    bid_no = _first_float(row, "best_bid_no", "no_bid")
    ask_no = _first_float(row, "best_ask_no", "no_ask")

    if mid is None and last is not None:
        mid = last
    if mid is None:
        mid = 0.5
        notes.append("missing mid/last price; defaulted to 0.5")

    if spread is None:
        spread = config.default_spread
        notes.append(f"missing spread; defaulted to {config.default_spread}")

    if bid_yes is None:
        bid_yes = _clamp_price(mid - spread / 2.0)
        notes.append("missing best bid; derived from mid-spread/2")
    if ask_yes is None:
        ask_yes = _clamp_price(mid + spread / 2.0)
        notes.append("missing best ask; derived from mid+spread/2")
    if bid_no is None:
        bid_no = _clamp_price(1.0 - ask_yes)
        notes.append("missing NO bid; derived as 1 - yes_ask")
    if ask_no is None:
        ask_no = _clamp_price(1.0 - bid_yes)
        notes.append("missing NO ask; derived as 1 - yes_bid")

    liquidity = _first_float(row, "liquidity", "liquidity_usd", "liquidity_dollars", "available_liquidity")
    if liquidity is None:
        volume = _first_float(row, "volume", "volume_usd", "volume_24h")
        liquidity = volume
        if liquidity is not None:
            notes.append("liquidity inferred from volume")
        else:
            notes.append("missing liquidity; no cap applied")

    status = _first_str(row, "market_status", "status", "market_state")
    expiry = _parse_timestamp(_first_raw(row, "expiry_time", "expiration_time", "close_time", "end_time", "expires_at"))
    quote_ts = _parse_timestamp(
        _first_raw(row, "quote_timestamp", "market_timestamp", "timestamp", "updated_at", "observed_at")
    )

    return MarketQuote(
        best_bid_yes=bid_yes,
        best_ask_yes=ask_yes,
        best_bid_no=bid_no,
        best_ask_no=ask_no,
        mid_price=mid,
        last_price=last,
        liquidity=liquidity,
        volume=_first_float(row, "volume", "volume_usd", "volume_24h"),
        market_status=status,
        expiry_time=expiry,
        quote_timestamp=quote_ts,
        metadata_notes=notes,
    )


def simulate_entry(
    *,
    row: dict[str, Any],
    side: str,
    requested_stake: float,
    config: PaperExecutionConfig,
) -> ExecutionResult:
    return _simulate(
        row=row,
        side=side,
        action="buy",
        requested_stake=requested_stake,
        config=config,
    )


def simulate_exit(
    *,
    row: dict[str, Any],
    side: str,
    shares: float,
    config: PaperExecutionConfig,
) -> ExecutionResult:
    notional = shares * _clamp_price(_first_float(row, "entry_price", "price") or 0.5)
    result = _simulate(
        row=row,
        side=side,
        action="sell",
        requested_stake=notional,
        config=config,
        shares_override=shares,
    )
    if result.fill_price is not None and result.status != "rejected":
        result.filled_stake = round(result.fill_price * shares, 6)
    return result


def _simulate(
    *,
    row: dict[str, Any],
    side: str,
    action: ExecutionAction,
    requested_stake: float,
    config: PaperExecutionConfig,
    shares_override: float | None = None,
) -> ExecutionResult:
    now = _as_utc(config.now or datetime.now(timezone.utc))
    quote = parse_market_quote(row, config=config)
    notes = list(quote.metadata_notes)

    rejection = _market_rejection(quote, now=now, config=config)
    if rejection:
        return ExecutionResult(
            status="rejected",
            fill_price=None,
            filled_stake=0.0,
            requested_stake=requested_stake,
            shares=0.0,
            slippage=0.0,
            spread_paid=0.0,
            quoted_mid=quote.mid_price,
            reason=rejection,
            metadata_notes=notes,
        )

    base_price, spread_paid = _reference_price(quote, side=side, action=action)
    slippage = base_price * (config.default_slippage_bps / 10_000.0)
    if action == "buy":
        fill_price = _clamp_price(base_price + slippage)
    else:
        fill_price = _clamp_price(base_price - slippage)

    filled_stake = requested_stake
    status: ExecutionStatus = "filled"
    reason: str | None = None

    if quote.liquidity is not None and quote.liquidity > 0:
        cap = quote.liquidity * config.liquidity_participation_cap
        if requested_stake > cap + 1e-9:
            if config.allow_partial_fills and cap > 0:
                filled_stake = round(cap, 6)
                status = "partial"
                reason = "partial_fill_liquidity_cap"
                notes.append(f"requested stake {requested_stake} exceeds liquidity cap {cap}")
            else:
                return ExecutionResult(
                    status="rejected",
                    fill_price=None,
                    filled_stake=0.0,
                    requested_stake=requested_stake,
                    shares=0.0,
                    slippage=0.0,
                    spread_paid=spread_paid,
                    quoted_mid=quote.mid_price,
                    reason="liquidity_cap_exceeded",
                    metadata_notes=notes,
                )

    if fill_price <= 0:
        return ExecutionResult(
            status="rejected",
            fill_price=None,
            filled_stake=0.0,
            requested_stake=requested_stake,
            shares=0.0,
            slippage=0.0,
            spread_paid=spread_paid,
            quoted_mid=quote.mid_price,
            reason="invalid_fill_price",
            metadata_notes=notes,
        )

    shares = shares_override if shares_override is not None else filled_stake / fill_price
    if shares_override is None:
        shares = filled_stake / fill_price
    shares = round(shares, 8)
    filled_stake = round(filled_stake, 6)

    return ExecutionResult(
        status=status,
        fill_price=round(fill_price, 6),
        filled_stake=filled_stake,
        requested_stake=round(requested_stake, 6),
        shares=shares,
        slippage=round(slippage, 6),
        spread_paid=round(spread_paid, 6),
        quoted_mid=quote.mid_price,
        reason=reason,
        metadata_notes=notes,
    )


def _market_rejection(quote: MarketQuote, *, now: datetime, config: PaperExecutionConfig) -> str | None:
    status = (quote.market_status or "").strip().lower()
    if status in {"closed", "settled", "resolved", "inactive", "halted"}:
        return "market_closed"

    if quote.expiry_time is not None and now >= quote.expiry_time:
        return "market_expired"

    if quote.quote_timestamp is not None:
        age = (now - quote.quote_timestamp).total_seconds()
        if age > config.max_market_data_age_seconds:
            return "stale_market_data"

    return None


def _reference_price(quote: MarketQuote, *, side: str, action: ExecutionAction) -> tuple[float, float]:
    normalized = str(side or "yes").strip().lower()
    is_no = normalized in {"no", "down", "short", "against"}
    if action == "buy":
        if is_no:
            base = quote.best_ask_no if quote.best_ask_no is not None else _clamp_price(1.0 - (quote.best_bid_yes or 0.5))
        else:
            base = quote.best_ask_yes if quote.best_ask_yes is not None else 0.5
        spread = (quote.best_ask_yes or base) - (quote.best_bid_yes or base)
    else:
        if is_no:
            base = quote.best_bid_no if quote.best_bid_no is not None else _clamp_price(1.0 - (quote.best_ask_yes or 0.5))
        else:
            base = quote.best_bid_yes if quote.best_bid_yes is not None else 0.5
        spread = (quote.best_ask_yes or base) - (quote.best_bid_yes or base)
    return _clamp_price(base), round(max(0.0, spread), 6)


def _clamp_price(value: float) -> float:
    return min(max(float(value), 0.01), 0.99)


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_str(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_raw(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
