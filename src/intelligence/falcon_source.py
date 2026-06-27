from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


FALCON_BASE_URL = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"

AGENT_TOP_TRADERS = 579
AGENT_FALCON_LEADERBOARD = 584
AGENT_WALLET_360 = 581
AGENT_LIFETIME_PERFORMANCE = 586
AGENT_PROFIT_AND_LOSS = 569
AGENT_POLYMARKET_MARKETS = 574
AGENT_KALSHI_MARKETS = 565
AGENT_POLYMARKET_TRADES = 556
AGENT_KALSHI_TRADES = 573
AGENT_ORDERBOOK = 572

_METRIC_FIELDS = (
    "h_score",
    "tier",
    "trajectory",
    "pnl",
    "roi",
    "win_rate",
    "drawdown",
    "sharpe_ratio",
    "risk_level",
    "combined_risk_score",
    "sybil_risk_flag",
    "suspicious_win_rate_flag",
    "market_concentration_ratio",
)


@dataclass(frozen=True)
class FalconStatus:
    enabled: bool
    base_url: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "base_url": self.base_url, "reason": self.reason}


class FalconClient:
    """Small read-only client for Falcon's parameterized API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        opener: Callable[[urllib.request.Request, float | None], Any] | None = None,
        timeout: float | None = 30,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("FALCON_API_KEY", "")
        self.base_url = base_url or os.getenv("FALCON_BASE_URL", FALCON_BASE_URL)
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(str(self.api_key or "").strip())

    def status(self) -> FalconStatus:
        if self.enabled:
            return FalconStatus(enabled=True, base_url=self.base_url)
        return FalconStatus(enabled=False, base_url=self.base_url, reason="missing FALCON_API_KEY")

    def retrieve(self, agent_id: int, params: dict[str, Any] | None = None, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        payload = self.build_payload(agent_id, params or {}, limit=limit, offset=offset)
        if not self.enabled:
            return {
                "disabled": True,
                "reason": "missing FALCON_API_KEY",
                "request": payload,
                "data": {"results": []},
                "pagination": {"limit": int(limit), "offset": int(offset), "has_more": False},
            }

        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Falcon request failed: {exc}") from exc
        return json.loads(body or "{}")

    def paginated_retrieve(
        self,
        agent_id: int,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 100,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max(1, int(max_pages))):
            response = self.retrieve(agent_id, params or {}, limit=limit, offset=offset)
            page = _results(response)
            rows.extend(page)
            pagination = response.get("pagination") or {}
            if not pagination.get("has_more") or not page:
                break
            offset += int(limit)
        return rows

    @staticmethod
    def build_payload(agent_id: int, params: dict[str, Any], *, limit: int, offset: int) -> dict[str, Any]:
        return {
            "agent_id": int(agent_id),
            "params": params,
            "pagination": {"limit": int(limit), "offset": int(offset)},
            "formatter_config": {"format_type": "raw"},
        }


class FalconWalletSource:
    def __init__(self, client: FalconClient | None = None) -> None:
        self.client = client or FalconClient()

    def fetch_leaderboard(self, *, limit: int = 50, sort_by: str = "h_score") -> list[dict[str, Any]]:
        response = self.client.retrieve(
            AGENT_FALCON_LEADERBOARD,
            {
                "min_win_rate_15d": "0.45",
                "max_win_rate_15d": "0.95",
                "min_roi_15d": "0",
                "min_total_trades_15d": "50",
                "max_total_trades_15d": "100000",
                "sort_by": sort_by,
            },
            limit=limit,
            offset=0,
        )
        return _results(response)

    def fetch_top_traders(self, *, limit: int = 50, period: str = "30d") -> list[dict[str, Any]]:
        response = self.client.retrieve(
            AGENT_TOP_TRADERS,
            {"wallet_address": "ALL", "leaderboard_period": period},
            limit=limit,
            offset=0,
        )
        return _results(response)

    def fetch_wallet_360(self, wallet: str, *, window_days: str = "15") -> list[dict[str, Any]]:
        response = self.client.retrieve(
            AGENT_WALLET_360,
            {"proxy_wallet": wallet, "window_days": str(window_days)},
            limit=100,
            offset=0,
        )
        return _results(response)

    def fetch_lifetime(self, wallet: str) -> list[dict[str, Any]]:
        response = self.client.retrieve(
            AGENT_LIFETIME_PERFORMANCE,
            {"wallet_address": wallet},
            limit=50,
            offset=0,
        )
        return _results(response)

    def normalize_wallet(self, row: dict[str, Any]) -> dict[str, Any]:
        wallet = _first(row, "wallet", "address", "proxy_wallet", "wallet_address")
        source_score = _first(row, "h_score", "source_score")
        source_rank = _first(row, "leaderboard_rank", "rank", "source_rank")
        metrics = _wallet_metrics(row)
        return {
            "wallet_address": wallet,
            "wallet_source": "falcon",
            "source_score": source_score,
            "source_rank": source_rank,
            "source_metrics_json": json.dumps(metrics, sort_keys=True),
            "validation_status": "pending",
            "paper_gate_status": "pending",
            "read_only": True,
        }

    def dry_run_candidates(self, *, limit: int = 50, sort_by: str = "h_score") -> dict[str, Any]:
        rows = self.fetch_leaderboard(limit=limit, sort_by=sort_by)
        candidates = [self.normalize_wallet(row) for row in rows if _first(row, "wallet", "address", "proxy_wallet", "wallet_address")]
        return {
            "read_only": True,
            "wallet_source": "falcon",
            "enabled": self.client.enabled,
            "status": self.client.status().to_dict(),
            "candidates": candidates,
        }


class FalconMarketSource:
    def __init__(self, client: FalconClient | None = None) -> None:
        self.client = client or FalconClient()

    def fetch_polymarket_market(
        self,
        *,
        condition_id: str | None = None,
        market_slug: str | None = None,
        closed: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if condition_id:
            params["condition_id"] = condition_id
        if market_slug:
            params["market_slug"] = market_slug
        if closed is not None:
            params["closed"] = "True" if closed else "False"
        return _results(self.client.retrieve(AGENT_POLYMARKET_MARKETS, params, limit=limit, offset=0))

    def fetch_polymarket_trades(
        self,
        *,
        wallet: str | None = None,
        condition_id: str | None = None,
        market_slug: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if wallet:
            params["proxy_wallet"] = wallet
        if condition_id:
            params["condition_id"] = condition_id
        if market_slug:
            params["market_slug"] = market_slug
        return _results(self.client.retrieve(AGENT_POLYMARKET_TRADES, params, limit=limit, offset=0))

    def normalize_outcome(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "market_slug": row.get("slug") or row.get("market_slug"),
            "condition_id": row.get("condition_id"),
            "winning_outcome": row.get("winning_outcome") or row.get("result"),
            "source": "falcon",
            "read_only": True,
            "source_metrics_json": json.dumps(row, sort_keys=True),
        }


class FalconCandidateStore:
    """Optional persistence for Falcon candidates; not used by the dry-run CLI."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wallet_source_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    wallet_source TEXT NOT NULL,
                    source_score REAL,
                    source_rank INTEGER,
                    source_metrics_json TEXT NOT NULL,
                    validation_status TEXT NOT NULL DEFAULT 'pending',
                    paper_gate_status TEXT NOT NULL DEFAULT 'pending',
                    read_only INTEGER NOT NULL DEFAULT 1,
                    discovered_at TEXT NOT NULL,
                    UNIQUE(wallet_address, wallet_source)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_source_candidates_source ON wallet_source_candidates(wallet_source, source_rank)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        from src.testing.runtime_db_redirect import resolve_runtime_db_target

        conn = sqlite3.connect(resolve_runtime_db_target(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_candidates(self, candidates: list[dict[str, Any]]) -> int:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO wallet_source_candidates
                    (wallet_address, wallet_source, source_score, source_rank, source_metrics_json,
                     validation_status, paper_gate_status, read_only, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, wallet_source) DO UPDATE SET
                    source_score = excluded.source_score,
                    source_rank = excluded.source_rank,
                    source_metrics_json = excluded.source_metrics_json,
                    validation_status = excluded.validation_status,
                    paper_gate_status = excluded.paper_gate_status,
                    read_only = excluded.read_only
                """,
                [
                    (
                        row["wallet_address"],
                        row["wallet_source"],
                        row.get("source_score"),
                        row.get("source_rank"),
                        row["source_metrics_json"],
                        row.get("validation_status", "pending"),
                        row.get("paper_gate_status", "pending"),
                        1 if row.get("read_only", True) else 0,
                        now,
                    )
                    for row in candidates
                ],
            )
        return len(candidates)


def _results(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or {}
    results = data.get("results", [])
    return [row for row in results if isinstance(row, dict)]


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _wallet_metrics(row: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "pnl": ("pnl", "total_pnl", "total_pnl_15d"),
        "roi": ("roi", "roi_pct", "roi_pct_15d"),
        "win_rate": ("win_rate", "win_rate_pct_15d"),
        "drawdown": ("drawdown", "max_drawdown"),
    }
    metrics: dict[str, Any] = {}
    for field in _METRIC_FIELDS:
        if field in aliases:
            value = _first(row, *aliases[field])
        else:
            value = row.get(field)
        if value is not None:
            metrics[field] = value
    return metrics
