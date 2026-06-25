from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath


@dataclass(frozen=True)
class RuntimeService:
    key: str
    label: str
    systemd_units: tuple[str, ...]
    path_patterns: tuple[str, ...]
    smoke_tags: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, path: str) -> bool:
        normalized = normalize_changed_path(path)
        return any(fnmatch(normalized, pattern) for pattern in self.path_patterns)


def normalize_changed_path(path: str) -> str:
    return str(PurePosixPath(str(path).replace("\\", "/"))).lstrip("./")


DEFAULT_RUNTIME_SERVICES: tuple[RuntimeService, ...] = (
    RuntimeService(
        key="telegram",
        label="Telegram console and reports",
        systemd_units=("polylens-telegram-console.service", "polylens-telegram-daily-report.service"),
        path_patterns=(
            "src/integrations/telegram*",
            "src/notifications/telegram.py",
            "tests/test_telegram*",
            "deploy/systemd/polylens-telegram-*",
        ),
        smoke_tags=("telegram-daily-report", "systemd"),
    ),
    RuntimeService(
        key="dashboard",
        label="Mission Control and dashboards",
        systemd_units=("polylens-dashboard.service", "polylens-trader-dashboard.service"),
        path_patterns=(
            "src/dashboard.py",
            "src/web/**",
            "deploy/grafana/**",
            "deploy/systemd/polylens-dashboard.service",
            "deploy/systemd/polylens-trader-dashboard.service",
            "tests/test_web*",
            "tests/test_mission_control*",
            "tests/test_trader_dashboard.py",
        ),
        smoke_tags=("dashboard-health", "systemd"),
    ),
    RuntimeService(
        key="wallet",
        label="Wallet autonomy and wallet intelligence",
        systemd_units=("wallet-autonomy.service",),
        path_patterns=(
            "src/intelligence/wallet*",
            "src/intelligence/real_wallet_ingestion.py",
            "src/intelligence/polymarket_leaderboard*",
            "src/analysis/wallet*",
            "src/analysis/trader_signal*",
            "src/analysis/trader_*",
            "deploy/systemd/wallet-autonomy.*",
            "tests/test_wallet*",
            "tests/test_trader_signal*",
            "tests/test_polymarket_leaderboard.py",
        ),
        smoke_tags=("wallet-service-health", "wallet-alpha-report", "wallet-alpha-rankings", "systemd"),
    ),
    RuntimeService(
        key="analytics",
        label="Analytics, scanners, paper services, and collectors",
        systemd_units=(
            "polylens-live-arb.service",
            "polylens-paper-trading.service",
            "polylens-prop-arb-collector.service",
            "polylens-prop-watch.service",
            "polylens-short-crypto-paper.service",
            "polylens-short-crypto-paper-settle.service",
            "polylens-trader-signal-cycle.service",
            "kalshi-market-recorder.service",
        ),
        path_patterns=(
            "src/adapters/**",
            "src/alerts/**",
            "src/analysis/**",
            "src/risk/**",
            "src/services/**",
            "src/storage/**",
            "src/trading/**",
            "deploy/systemd/polylens-live-arb.*",
            "deploy/systemd/polylens-paper-trading.*",
            "deploy/systemd/polylens-prop-*",
            "deploy/systemd/polylens-short-crypto-*",
            "deploy/systemd/polylens-trader-signal-cycle.*",
            "deploy/systemd/kalshi-market-recorder.*",
            "tests/test_paper*",
            "tests/test_prop*",
            "tests/test_kalshi*",
            "tests/test_crypto*",
            "tests/test_risk*",
        ),
        smoke_tags=("database-connectivity", "systemd"),
    ),
)


class ServiceMapper:
    def __init__(self, services: tuple[RuntimeService, ...] = DEFAULT_RUNTIME_SERVICES) -> None:
        self.services = services

    def affected_services(self, changed_files: list[str] | tuple[str, ...]) -> list[dict[str, object]]:
        matched: list[RuntimeService] = []
        for service in self.services:
            if any(service.matches(path) for path in changed_files):
                matched.append(service)
        return [
            {
                "key": service.key,
                "label": service.label,
                "systemd_units": list(service.systemd_units),
                "smoke_tags": list(service.smoke_tags),
            }
            for service in matched
        ]

    def affected_units(self, changed_files: list[str] | tuple[str, ...]) -> list[str]:
        units: list[str] = []
        for service in self.affected_services(changed_files):
            for unit in service["systemd_units"]:
                if str(unit) not in units:
                    units.append(str(unit))
        return units

    def smoke_tags(self, changed_files: list[str] | tuple[str, ...]) -> list[str]:
        tags: list[str] = []
        for service in self.affected_services(changed_files):
            for tag in service["smoke_tags"]:
                if str(tag) not in tags:
                    tags.append(str(tag))
        return tags
