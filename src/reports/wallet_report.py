from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WalletReport:
    wallet_address: str
    username: str | None
    trade_count: int
    volume: float
    average_trade_size: float
    largest_trade: dict[str, Any] | None
    largest_trade_size: float
    favorite_markets: list[dict[str, Any]]
    favorite_categories: list[dict[str, Any]]
    market_concentration: str
    trading_schedule: dict[str, Any]
    estimated_pnl: float
    exposure: float
    position_sizing: dict[str, Any]
    behavior_classification: str
    confidence_score: float
    signals: dict[str, Any]
    raw_counts: dict[str, int]
    cross_platform_arbitrage_candidates: list[dict[str, Any]] = field(default_factory=list)
    price_aware_arbitrage_candidates: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def save(self, reports_dir: str | Path = "data/reports") -> Path:
        path = Path(reports_dir)
        path.mkdir(parents=True, exist_ok=True)
        output = path / f"{self.wallet_address.lower()}_wallet_report.json"
        output.write_text(self.to_json() + "\n", encoding="utf-8")
        return output

    def summary_text(self) -> str:
        largest_title = (self.largest_trade or {}).get("title") or "n/a"
        hour = self.trading_schedule.get("most_active_periods", {}).get("hour_utc")
        weekday = self.trading_schedule.get("most_active_periods", {}).get("weekday_utc")
        lines = [
            "Polylens Wallet Report",
            "=" * 24,
            f"Wallet Address: {self.wallet_address}",
            f"Username: {self.username or 'Unknown'}",
            f"Trade Count: {self.trade_count}",
            f"Volume: ${self.volume:,.2f}",
            f"Average Trade Size: ${self.average_trade_size:,.2f}",
            f"Largest Trade: ${self.largest_trade_size:,.2f} - {largest_title}",
            f"Favorite Markets: {', '.join(item['market'] for item in self.favorite_markets[:3]) or 'n/a'}",
            f"Favorite Categories: {', '.join(item['category'] for item in self.favorite_categories[:3]) or 'n/a'}",
            f"Market Concentration: {self.market_concentration}",
            f"Trading Schedule: {weekday or 'n/a'} at {hour if hour is not None else 'n/a'}:00 UTC",
            f"Estimated PnL: ${self.estimated_pnl:,.2f}",
            f"Exposure: ${self.exposure:,.2f}",
            f"Behavior Classification: {self.behavior_classification}",
            f"Confidence Score: {self.confidence_score:.2f}",
        ]
        return "\n".join(lines)
