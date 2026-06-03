from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


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
    def __init__(self) -> None:
        self.orders: list[PaperOrder] = []

    def place_order(self, ticker: str, side: str, price: float, count: int) -> PaperOrder:
        order = PaperOrder(
            ticker=ticker,
            side=side.lower(),
            price=float(price),
            count=int(count),
            status="filled_paper",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.orders.append(order)
        return order

    def status(self) -> dict[str, Any]:
        exposure = sum(order.price * order.count for order in self.orders)
        return {"mode": "paper", "orders": len(self.orders), "open_exposure": round(exposure, 4)}
