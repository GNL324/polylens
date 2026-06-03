from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PAPER_JOURNAL = "data/raw/kalshi_paper_orders.jsonl"


@dataclass
class PaperOrder:
    ticker: str
    side: str
    price: float
    count: int
    status: str
    created_at: str
    mode: str = "paper"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperPortfolio:
    def __init__(self, journal_path: str | Path | None = DEFAULT_PAPER_JOURNAL) -> None:
        self.orders: list[PaperOrder] = []
        self.journal_path = Path(journal_path) if journal_path else None

    def place_order(self, ticker: str, side: str, price: float, count: int) -> PaperOrder:
        order = PaperOrder(
            ticker=ticker,
            side=side.lower(),
            price=round(float(price), 4),
            count=int(count),
            status="filled_paper",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.orders.append(order)
        self._journal_order(order)
        return order

    def status(self) -> dict[str, Any]:
        exposure = sum(order.price * order.count for order in self.orders)
        return {
            "mode": "paper",
            "orders": len(self.orders),
            "open_exposure": round(exposure, 4),
            "journal_path": str(self.journal_path) if self.journal_path else None,
        }

    def _journal_order(self, order: PaperOrder) -> None:
        if self.journal_path is None:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(order.to_dict(), sort_keys=True) + "\n")
