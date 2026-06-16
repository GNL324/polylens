from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)

DEFAULT_DATA_API_BASE = "https://data-api.polymarket.com"
LEADERBOARD_ENDPOINT = "v1/leaderboard"
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

LEADERBOARD_CATEGORIES = (
    "OVERALL",
    "POLITICS",
    "SPORTS",
    "CRYPTO",
    "CULTURE",
    "MENTIONS",
    "WEATHER",
    "ECONOMICS",
    "TECH",
    "FINANCE",
)
LEADERBOARD_TIME_PERIODS = ("DAY", "WEEK", "MONTH", "ALL")
LEADERBOARD_ORDER_BY = ("PNL", "VOL")


class PolymarketLeaderboardAPIError(RuntimeError):
    """Raised when the public leaderboard API returns an error."""


@dataclass(frozen=True)
class LeaderboardEntry:
    rank: int
    proxy_wallet: str
    user_name: str
    vol: float
    pnl: float
    x_username: str = ""
    verified_badge: bool = False
    profile_image: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LeaderboardFetchRequest:
    category: str = "OVERALL"
    time_period: str = "MONTH"
    order_by: str = "PNL"
    limit: int = 50
    offset: int = 0

    def to_params(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "timePeriod": self.time_period,
            "orderBy": self.order_by,
            "limit": max(1, min(int(self.limit), 50)),
            "offset": max(0, int(self.offset)),
        }


def parse_leaderboard_entry(row: dict[str, Any]) -> LeaderboardEntry | None:
    wallet = str(row.get("proxyWallet") or row.get("proxy_wallet") or "").strip().lower()
    if not WALLET_RE.match(wallet):
        return None
    try:
        rank = int(row.get("rank") or 0)
    except (TypeError, ValueError):
        rank = 0
    return LeaderboardEntry(
        rank=rank,
        proxy_wallet=wallet,
        user_name=str(row.get("userName") or row.get("user_name") or ""),
        vol=float(row.get("vol") or 0.0),
        pnl=float(row.get("pnl") or 0.0),
        x_username=str(row.get("xUsername") or row.get("x_username") or ""),
        verified_badge=bool(row.get("verifiedBadge") or row.get("verified_badge")),
        profile_image=str(row.get("profileImage") or row.get("profile_image") or ""),
        metadata={
            key: value
            for key, value in row.items()
            if key
            not in {
                "rank",
                "proxyWallet",
                "proxy_wallet",
                "userName",
                "user_name",
                "vol",
                "pnl",
                "xUsername",
                "x_username",
                "verifiedBadge",
                "verified_badge",
                "profileImage",
                "profile_image",
            }
        },
    )


def parse_leaderboard_payload(payload: Any) -> list[LeaderboardEntry]:
    if not isinstance(payload, list):
        return []
    entries: list[LeaderboardEntry] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        entry = parse_leaderboard_entry(row)
        if entry is not None:
            entries.append(entry)
    return entries


class PolymarketLeaderboardClient:
    """Read-only client for the public Polymarket leaderboard API."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_DATA_API_BASE,
        timeout: float = 30.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urlopen

    def fetch_leaderboard(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 50,
        offset: int = 0,
    ) -> list[LeaderboardEntry]:
        request = LeaderboardFetchRequest(
            category=category,
            time_period=time_period,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )
        payload = self._get_json(LEADERBOARD_ENDPOINT, request.to_params())
        entries = parse_leaderboard_payload(payload)
        LOGGER.info(
            "leaderboard fetch category=%s timePeriod=%s orderBy=%s rows=%s",
            category,
            time_period,
            order_by,
            len(entries),
        )
        return entries

    def _get_json(self, endpoint: str, params: dict[str, Any]) -> Any:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request = Request(url, headers={"User-Agent": "polylens/0.1", "Accept": "application/json"})
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else []
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise PolymarketLeaderboardAPIError(
                f"GET {url} failed with HTTP {exc.code}: {detail[:200]}"
            ) from exc
        except Exception as exc:
            raise PolymarketLeaderboardAPIError(f"GET {url} failed: {exc}") from exc
